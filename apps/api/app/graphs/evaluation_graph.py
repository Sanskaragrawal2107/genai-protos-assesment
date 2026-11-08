"""LangGraph workflow: job → retrieval → verification → evaluation → ranking.

    structure ─▶ retrieve ─▶ (empty?) ─▶ END
                    │
                    ▼
                 verify ─▶ score ─▶ rank ─▶ END

``verify`` performs the external GitHub/LeetCode lookups; ``score`` is pure
deterministic Python (with an optional, clamped LLM refinement); ``rank`` sorts
by score and evidence. Persistence happens inside ``score`` so a re-run
refreshes rather than duplicates rows.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from ..services import evaluation as ev
from ..services.verification import verify_candidate

log = logging.getLogger("recruiter.graph.evaluation")


class EvaluationState(TypedDict, total=False):
    description: str
    title: str | None
    requirements: Any
    requirement_method: str
    candidates: list[dict[str, Any]]
    verification: dict[str, dict[str, Any]]
    scored: list[dict[str, Any]]
    ranked: list[dict[str, Any]]
    errors: list[str]


def build_evaluation_graph(
    session: AsyncSession, *, workspace_id: str, job_id: str, limit: int = 25, verify: bool = True
):
    async def structure(state: EvaluationState) -> dict[str, Any]:
        requirements, method = await ev.structure_requirements(
            state["description"], title=state.get("title")
        )
        return {"requirements": requirements, "requirement_method": method}

    async def retrieve(state: EvaluationState) -> dict[str, Any]:
        rows = await ev.retrieve(session, workspace_id, state["requirements"], limit=limit)
        return {"candidates": rows}

    async def verify_node(state: EvaluationState) -> dict[str, Any]:
        requirements = state["requirements"]
        keywords = list(requirements.required_skills) + list(requirements.preferred_skills)
        evidence: dict[str, dict[str, Any]] = {}
        errors = list(state.get("errors") or [])
        if not verify:
            for row in state.get("candidates") or []:
                evidence[row["id"]] = {
                    "github": {"available": False, "reason": "verification disabled for this run"},
                    "leetcode": {"available": False, "reason": "verification disabled for this run"},
                    "evidence_score": 0.0,
                    "signals": ["Public verification skipped (verification disabled)"],
                }
            return {"verification": evidence, "errors": errors}
        async def _verify_one(row: dict[str, Any]):
            try:
                ev_data = await verify_candidate(row, keywords=keywords)
                return row["id"], ev_data, None
            except Exception as exc:  # noqa: BLE001 - evidence failures must not break ranking
                log.warning("verification failed for %s: %s", row["id"], exc.__class__.__name__)
                fallback = {
                    "github": {"available": False, "reason": "verification error"},
                    "leetcode": {"available": False, "reason": "verification error"},
                    "evidence_score": 0.0,
                    "signals": ["Verification unavailable"],
                }
                return row["id"], fallback, f"verification error for {row['id']}: {exc.__class__.__name__}"

        results = await asyncio.gather(*[_verify_one(row) for row in state.get("candidates") or []])
        for cid, ev_data, err in results:
            evidence[cid] = ev_data
            if err:
                errors.append(err)
        return {"verification": evidence, "errors": errors}

    async def score(state: EvaluationState) -> dict[str, Any]:
        requirements = state["requirements"]
        evidence = state.get("verification") or {}
        candidates_list = state.get("candidates") or []
        if not candidates_list:
            return {"scored": []}

        sem = asyncio.Semaphore(5)

        async def _eval_one(row: dict[str, Any]):
            async with sem:
                verification = evidence.get(row["id"]) or {
                    "github": {"available": False, "reason": "not verified"},
                    "leetcode": {"available": False, "reason": "not verified"},
                    "evidence_score": 0.0,
                    "signals": [],
                }
                base = await ev.evaluate_candidate_llm(row, requirements, verification)
                return row, verification, base

        eval_results = await asyncio.gather(*[_eval_one(row) for row in candidates_list])

        scored: list[dict[str, Any]] = []
        for row, verification, base in eval_results:
            await _persist(row, requirements, verification, base)
            scored.append(
                {
                    "candidate_id": row["id"],
                    "full_name": row.get("full_name"),
                    "verification": verification,
                    **{k: base[k] for k in (
                        "match_score", "skills_score", "experience_score", "project_score",
                        "location_score", "evidence_score", "strengths",
                        "missing_requirements", "concerns", "explanation",
                    )},
                    "method": base.get("method", "llm"),
                    "candidate": _summary(row, base["match_score"]),
                }
            )
        return {"scored": scored}

    async def _persist(row: dict[str, Any], requirements, verification, base) -> None:
        from ..db import candidate_evaluations, utcnow
        import sqlalchemy as sa
        import uuid

        existing = (
            await session.execute(
                sa.select(candidate_evaluations.c.id).where(
                    candidate_evaluations.c.candidate_id == row["id"],
                    candidate_evaluations.c.job_id == job_id,
                )
            )
        ).scalar_one_or_none()
        values = {
            "match_score": base["match_score"],
            "skills_score": base["skills_score"],
            "experience_score": base["experience_score"],
            "project_score": base["project_score"],
            "location_score": base["location_score"],
            "evidence_score": base["evidence_score"],
            "weights": base["weights"],
            "evaluation_details": {
                "matched_required_skills": base.get("matched_required_skills", []),
                "matched_preferred_skills": base.get("matched_preferred_skills", []),
                "signals": verification.get("signals", []),
                "requirements": requirements.model_dump(mode="json"),
            },
            "strengths": base["strengths"],
            "missing_requirements": base["missing_requirements"],
            "concerns": base["concerns"],
            "verification_evidence": verification,
            "explanation": base["explanation"],
            "method": base.get("method", "heuristic"),
            "input_hash": ev._input_hash(requirements, row, verification),
            "evaluated_at": utcnow(),
        }
        if existing:
            await session.execute(
                sa.update(candidate_evaluations)
                .where(candidate_evaluations.c.id == existing)
                .values(**values)
            )
        else:
            await session.execute(
                sa.insert(candidate_evaluations).values(
                    id=str(uuid.uuid4()),
                    workspace_id=workspace_id,
                    candidate_id=row["id"],
                    job_id=job_id,
                    **values,
                )
            )
        await session.commit()

    def _summary(row: dict[str, Any], score: float) -> dict[str, Any]:
        from ..repo import candidate_summary

        item = candidate_summary(row)
        item["match_score"] = score
        return item

    async def rank(state: EvaluationState) -> dict[str, Any]:
        scored = list(state.get("scored") or [])
        scored.sort(
            key=lambda item: (
                item["match_score"],
                item.get("evidence_score", 0),
                (item.get("candidate") or {}).get("experience_years") or 0,
            ),
            reverse=True,
        )
        return {"ranked": scored}

    graph = StateGraph(EvaluationState)
    graph.add_node("structure", structure)
    graph.add_node("retrieve", retrieve)
    graph.add_node("verify", verify_node)
    graph.add_node("score", score)
    graph.add_node("rank", rank)

    graph.set_entry_point("structure")
    graph.add_edge("structure", "retrieve")
    graph.add_conditional_edges(
        "retrieve",
        lambda state: "verify" if state.get("candidates") else "done",
        {"verify": "verify", "done": END},
    )
    graph.add_edge("verify", "score")
    graph.add_edge("score", "rank")
    graph.add_edge("rank", END)
    return graph.compile()


async def run_evaluation(
    *,
    session: AsyncSession,
    workspace_id: str,
    job_id: str,
    description: str,
    title: str | None = None,
    limit: int = 25,
    verify: bool = True,
) -> dict[str, Any]:
    compiled = build_evaluation_graph(
        session, workspace_id=workspace_id, job_id=job_id, limit=limit, verify=verify
    )
    result = await compiled.ainvoke(
        {"description": description, "title": title, "errors": []},
    )
    return {
        "requirements": result.get("requirements"),
        "requirement_method": result.get("requirement_method", "heuristic"),
        "ranked": result.get("ranked") or [],
        "candidate_pool": len(result.get("candidates") or []),
        "errors": result.get("errors") or [],
    }
