"""Job understanding, hybrid retrieval and explainable ranking."""
from __future__ import annotations

import sqlalchemy as sa

from app import db as db_module
from app.db import candidates, jobs, utcnow
from app.graphs.evaluation_graph import run_evaluation
from app.schemas import JobRequirements
from support import SAMPLE_JOB_DESCRIPTION
from app.services.evaluation import heuristic_requirements, score_candidate, structure_requirements

CANDIDATES = [
    {
        "full_name": "Strong AI Engineer",
        "email": "strong@example.com",
        "location": "Bengaluru, India",
        "experience_years": 5.0,
        "current_role": "Senior AI Engineer",
        "skills": ["Python", "FastAPI", "LangGraph", "RAG", "PostgreSQL", "AWS"],
        "projects": [{"name": "Recruiting Copilot", "description": "LangGraph agent screening resumes with RAG over pgvector"}],
        "github_url": "https://github.com/octocat",
        "leetcode_url": "https://leetcode.com/leetcode",
        "summary": "Production RAG and agent workflows",
    },
    {
        "full_name": "Adjacent Backend Engineer",
        "email": "backend@example.com",
        "location": "Pune, India",
        "experience_years": 3.0,
        "current_role": "Backend Engineer",
        "skills": ["Python", "Django", "PostgreSQL", "Redis", "Docker"],
        "projects": [{"name": "Ledger", "description": "payments ledger service"}],
        "github_url": None,
        "leetcode_url": None,
        "summary": "Backend services",
    },
    {
        "full_name": "Junior Frontend",
        "email": "frontend@example.com",
        "location": "Remote",
        "experience_years": 1.0,
        "current_role": "Frontend Engineer",
        "skills": ["TypeScript", "React", "Tailwind"],
        "projects": [],
        "github_url": None,
        "leetcode_url": None,
        "summary": "UI development",
    },
]


async def _insert(db, workspace_id: str) -> list[dict]:
    out = []
    for item in CANDIDATES:
        cid = db_module.new_id()
        now = utcnow()
        row = {
            "id": cid,
            "workspace_id": workspace_id,
            "full_name": item["full_name"],
            "email": item["email"],
            "email_normalized": item["email"],
            "phone": None,
            "location": item["location"],
            "preferred_location": None,
            "experience_years": item["experience_years"],
            "current_role": item["current_role"],
            "current_company": None,
            "previous_companies": [],
            "education": [],
            "university": "Test University",
            "graduation_year": 2019,
            "skills": item["skills"],
            "programming_languages": [],
            "frameworks": [],
            "tools": [],
            "projects": item["projects"],
            "summary": item["summary"],
            "github_url": item["github_url"],
            "leetcode_url": item["leetcode_url"],
            "linkedin_url": None,
            "portfolio_url": None,
            "resume_storage_path": None,
            "resume_filename": None,
            "resume_text": None,
            "resume_sha256": None,
            "extraction_method": "heuristic",
            "extraction_confidence": 0.8,
            "extraction": {},
            "pipeline_status": "APPLIED",
            "source": "test",
            "owner_id": None,
            "created_at": now,
            "updated_at": now,
        }
        await db.execute(sa.insert(candidates).values(**row))
        out.append(row)
    await db.commit()
    return out


def test_heuristic_requirements_extracts_structure():
    req = heuristic_requirements(SAMPLE_JOB_DESCRIPTION, "Senior AI Engineer")
    lowered = [s.lower() for s in req.required_skills]
    assert "python" in lowered
    assert "fastapi" in lowered
    assert any("langgraph" in s for s in lowered)
    assert req.min_experience_years == 3
    assert req.required_location and "bengaluru" in req.required_location.lower()
    assert req.responsibilities


async def test_scoring_is_deterministic_and_explainable():
    req = JobRequirements(
        required_skills=["Python", "FastAPI", "LangGraph", "RAG", "PostgreSQL"],
        preferred_skills=["Docker"],
        min_experience_years=3,
        required_location="Bengaluru",
        weights={"skills": 0.38, "experience": 0.2, "projects": 0.22, "location": 0.08, "evidence": 0.12},
    )
    candidate = dict(CANDIDATES[0])
    candidate["updated_at"] = None
    verification = {"available": False, "evidence_score": 0, "signals": []}

    first = score_candidate(candidate, req, verification)
    second = score_candidate(candidate, req, verification)
    assert first == second
    assert first["match_score"] > 60
    assert first["skills_score"] > 80
    assert first["explanation"]
    assert any("required skills" in s for s in first["strengths"])

    weak = dict(CANDIDATES[2])
    weak["updated_at"] = None
    weak_score = score_candidate(weak, req, verification)
    assert weak_score["match_score"] < first["match_score"]
    assert any("Missing required skill" in m for m in weak_score["missing_requirements"])


async def test_end_to_end_evaluation_ranks_and_persists(db, workspace):
    rows = await _insert(db, workspace["workspace_id"])
    job_id = db_module.new_id()
    now = utcnow()
    await db.execute(
        sa.insert(jobs).values(
            id=job_id,
            workspace_id=workspace["workspace_id"],
            title="Senior AI Engineer",
            description=SAMPLE_JOB_DESCRIPTION,
            structured_requirements={},
            status="open",
            created_by="tester",
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    result = await run_evaluation(
        session=db,
        workspace_id=workspace["workspace_id"],
        job_id=job_id,
        description=SAMPLE_JOB_DESCRIPTION,
        title="Senior AI Engineer",
        limit=25,
        verify=False,
    )
    ranked = result["ranked"]
    # The 1-year frontend profile is excluded by the objective SQL filter
    # (minimum 3 years, with 1 year of headroom), so only 2 candidates are scored.
    assert len(ranked) == 2
    assert result["candidate_pool"] == 2
    names = [item["full_name"] for item in ranked]
    assert "Junior Frontend" not in names
    assert ranked[0]["full_name"] == "Strong AI Engineer"
    assert ranked[0]["match_score"] >= ranked[1]["match_score"]
    assert ranked[-1]["missing_requirements"]

    # persisted for the UI and re-runs upsert instead of duplicating
    count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(db_module.candidate_evaluations)
            .where(db_module.candidate_evaluations.c.job_id == job_id)
        )
    ).scalar_one()
    assert count == len(ranked)

    await run_evaluation(
        session=db,
        workspace_id=workspace["workspace_id"],
        job_id=job_id,
        description=SAMPLE_JOB_DESCRIPTION,
        title="Senior AI Engineer",
        verify=False,
    )
    count_again = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(db_module.candidate_evaluations)
            .where(db_module.candidate_evaluations.c.job_id == job_id)
        )
    ).scalar_one()
    assert count_again == len(ranked)


async def test_requirements_via_llm():
    req, method = await structure_requirements("We need Python and FastAPI. 2+ years.", title="Backend")
    assert method == "llm"
    assert any("python" in s.lower() for s in req.required_skills)


def test_claims_are_not_promoted_to_verified_facts():
    req = JobRequirements(required_skills=["Rust"], min_experience_years=5)
    candidate = dict(CANDIDATES[2])
    candidate["updated_at"] = None
    result = score_candidate(
        candidate,
        req,
        {
            "github": {"available": False, "reason": "No GitHub profile URL on the resume"},
            "leetcode": {"available": False, "reason": "No LeetCode profile URL on the resume"},
            "evidence_score": 0,
            "signals": ["GitHub not verified: no profile"],
        },
    )
    assert result["evidence_score"] == 0
    assert any("GitHub not verified" in c for c in result["concerns"])
    assert not any("Rust" in s for s in result["strengths"])
