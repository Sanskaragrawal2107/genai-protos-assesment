"""Job description understanding, hybrid retrieval and explainable scoring.

Pipeline (mirrored by the LangGraph workflows in ``app/graphs``):

1. ``structure_requirements`` – LLM (or deterministic parser) turns the JD text
   into typed criteria.
2. ``retrieve``              – Postgres does the objective filtering (experience,
   location, status) and a skill-overlap pre-rank. No keyword-only "search".
3. ``score_candidate``       – deterministic, reproducible component scores.
4. ``evaluate_candidate_llm``– optional LLM pass that may nudge component scores
   only inside a clamped band and must justify each change with facts present in
   the input (see ``app/graphs/evaluation_graph.py``).
5. ``rank``                  – final ordering with explicit tie-breakers.

Scores are always decomposable: the UI shows every component and the evidence
behind it, and distinguishes resume claims from publicly verified signals.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlalchemy as sa
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import candidates, utcnow
from ..llm import LLMError, LLMUnavailable, llm, wrap_document
from ..repo import DEFAULT_WEIGHTS
from ..schemas import JobRequirements

log = logging.getLogger("recruiter.evaluation")

SKILL_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "react.js": "react",
    "reactjs": "react",
    "nextjs": "next.js",
    "nodejs": "node.js",
    "node": "node.js",
    "golang": "go",
    "ml": "machine learning",
    "k8s": "kubernetes",
    "rest": "rest api",
    "restful": "rest api",
    "llms": "llm",
    "vector search": "rag",
    "retrieval augmented generation": "rag",
    "genai": "llm",
    "ci cd": "ci/cd",
}

_KNOWN_SKILLS = [
    "python", "java", "javascript", "typescript", "go", "rust", "c++", "c#", "sql", "kotlin",
    "fastapi", "flask", "django", "node.js", "express", "next.js", "react", "angular", "vue",
    "spring", "rails", "langgraph", "langchain", "llamaindex", "rag", "llm", "pytorch",
    "tensorflow", "scikit-learn", "nlp", "computer vision", "hugging face", "postgresql",
    "mysql", "mongodb", "redis", "elasticsearch", "pinecone", "qdrant", "pgvector", "kafka",
    "rabbitmq", "celery", "airflow", "spark", "docker", "kubernetes", "terraform", "aws",
    "gcp", "azure", "supabase", "firebase", "graphql", "rest api", "microservices",
    "system design", "mlops", "ci/cd", "git", "linux", "prompt engineering",
]


SKILL_DISPLAY = {
    "fastapi": "FastAPI", "langgraph": "LangGraph", "langchain": "LangChain",
    "postgresql": "PostgreSQL", "rag": "RAG", "llm": "LLM", "aws": "AWS", "gcp": "GCP",
    "azure": "Azure", "typescript": "TypeScript", "javascript": "JavaScript",
    "github": "GitHub", "nlp": "NLP", "mlops": "MLOps", "next.js": "Next.js",
    "node.js": "Node.js", "rest api": "REST API", "ci/cd": "CI/CD", "sql": "SQL",
    "hugging face": "Hugging Face", "scikit-learn": "scikit-learn", "go": "Go",
    "prompt engineering": "Prompt Engineering", "microservices": "Microservices",
    "machine learning": "Machine Learning", "computer vision": "Computer Vision",
    "system design": "System Design", "docker": "Docker", "kubernetes": "Kubernetes",
    "pgvector": "pgvector", "supabase": "Supabase", "graphql": "GraphQL",
}


def display_skill(value: str) -> str:
    key = value.strip().lower()
    return SKILL_DISPLAY.get(key, value if not value.islower() or len(value) <= 2 else value.title())


def normalize_skill(value: str) -> str:
    key = re.sub(r"\s+", " ", value.strip().lower())
    return SKILL_ALIASES.get(key, key)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9+#.]+", text.lower()) if len(t) > 1}


def _contains(haystack_tokens: set[str], haystack_text: str, skill: str) -> bool:
    skill = normalize_skill(skill)
    if not skill:
        return False
    if skill in haystack_text:
        return True
    return skill in haystack_tokens or all(part in haystack_tokens for part in skill.split())


# ---------------------------------------------------------------- JD parsing
_SYSTEM = (
    "You are a job-description analysis engine. Convert the job description into "
    "strict JSON criteria used by a candidate scoring system. Extract only what the "
    "description states; do not add generic requirements. Keep skills as short "
    "canonical names (e.g. 'PostgreSQL', 'FastAPI', 'LangGraph'). Put soft or "
    "secondary asks in preferred_skills. responsibilities are concrete duties."
)

_SCHEMA = """{
  "title": string|null,
  "required_skills": string[], "preferred_skills": string[],
  "min_experience_years": number|null, "preferred_experience_years": number|null,
  "required_location": string|null, "locations": string[],
  "education": string|null, "required_projects": string[],
  "responsibilities": string[], "qualifications": string[], "keywords": string[],
  "weights": {"skills": number, "experience": number, "projects": number, "location": number, "evidence": number}
}"""


def _section_lines(text: str, headings: tuple[str, ...]) -> list[str]:
    lines = [line.strip(" •-\t") for line in text.splitlines()]
    capture = False
    out: list[str] = []
    for line in lines:
        low = line.lower().strip(":")
        if any(low.startswith(h) for h in headings):
            capture = True
            remainder = re.split(r"[:\-]", line, maxsplit=1)
            if len(remainder) == 2 and remainder[1].strip():
                out.append(remainder[1].strip())
            continue
        if capture:
            if not line:
                if out:
                    break
                continue
            if len(line) < 80 and line.isupper():
                break
            if any(low.startswith(h) for h in ("responsibilities", "requirements", "qualifications", "about", "what you")):
                break
            out.append(line)
    return out


def heuristic_requirements(description: str, title: str | None = None) -> JobRequirements:
    lowered = description.lower()
    required = [s for s in _KNOWN_SKILLS if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", lowered)]

    explicit_required = _section_lines(description, ("required skills", "must have", "requirements", "required"))
    explicit_preferred = _section_lines(description, ("preferred skills", "nice to have", "bonus", "good to have"))
    responsibilities = _section_lines(description, ("responsibilities", "what you will do", "the role"))

    preferred_only: list[str] = []
    for line in explicit_preferred:
        for skill in _KNOWN_SKILLS:
            if re.search(rf"(?<![a-z]){re.escape(skill)}(?![a-z])", line.lower()):
                preferred_only.append(skill)

    exp_match = re.search(r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*\+?\s*years?", lowered)
    if not exp_match:
        exp_match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years?", lowered)
    min_exp = float(exp_match.group(1)) if exp_match else None

    location = None
    loc_match = re.search(r"location\s*[:\-]\s*([^\n]{2,60})", lowered)
    if loc_match:
        raw_location = loc_match.group(1).strip(" .")
        city_match = re.search(
            r"\b(bengaluru|bangalore|mumbai|delhi|noida|gurgaon|hyderabad|pune|chennai|kolkata|"
            r"london|berlin|new york|san francisco|singapore|dubai|remote)\b",
            raw_location,
        )
        if city_match:
            location = city_match.group(1).title()
            if "remote" in raw_location and "remote" not in location.lower():
                location += " or remote"
        else:
            location = raw_location.split(" or ")[0].split(",")[0].strip()[:40]
    locations = []
    for city in ("bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune", "chennai",
                 "london", "berlin", "new york", "san francisco", "remote", "singapore", "dubai"):
        if city in lowered:
            locations.append(city.title())

    education = None
    edu_match = re.search(r"(bachelor[^.\n]{0,80}|master[^.\n]{0,80}|b\.?tech[^.\n]{0,60}|m\.?tech[^.\n]{0,60})",
                          description, re.IGNORECASE)
    if edu_match:
        education = edu_match.group(1).strip()

    required_set = [s for s in required if s not in preferred_only]
    if not required_set and explicit_preferred:
        required_set = required
    return JobRequirements(
        title=title,
        required_skills=[display_skill(s) for s in required_set][:18],
        preferred_skills=[display_skill(s) for s in dict.fromkeys(preferred_only)][:12],
        min_experience_years=min_exp,
        preferred_experience_years=(min_exp + 1) if min_exp else None,
        required_location=location,
        locations=locations,
        education=education,
        required_projects=[],
        responsibilities=[r[:200] for r in responsibilities][:8],
        qualifications=[q[:200] for q in explicit_required][:8],
        keywords=list(dict.fromkeys([*required_set, *preferred_only]))[:20],
        weights=dict(DEFAULT_WEIGHTS),
    )


async def structure_requirements(description: str, *, title: str | None = None) -> tuple[JobRequirements, str]:
    if not llm.enabled:
        raise RuntimeError("LLM is required for structuring job requirements but is not enabled")
    prompt = (
        f"Job title hint: {title or 'unknown'}\nSchema: {_SCHEMA}\n\n"
        + wrap_document(description, source="job_description")
    )
    try:
        raw = await llm.complete_json(system=_SYSTEM, user=prompt, max_tokens=1500)
        # Ensure weights contain valid numeric floats and fallback to DEFAULT_WEIGHTS
        weights = raw.get("weights")
        if isinstance(weights, dict):
            cleaned_weights = {k: float(v) for k, v in weights.items() if isinstance(v, (int, float))}
            raw["weights"] = {**dict(DEFAULT_WEIGHTS), **cleaned_weights}
        else:
            raw["weights"] = dict(DEFAULT_WEIGHTS)
        req = JobRequirements.model_validate(raw)
        if not req.title:
            req.title = title
        return req, "llm"
    except (LLMUnavailable, LLMError, ValueError) as exc:
        log.error("JD structuring with LLM failed: %s", exc)
        raise RuntimeError(f"LLM job structuring failed: {exc}") from exc


# ------------------------------------------------------------------ retrieval
async def retrieve(
    session: AsyncSession,
    workspace_id: str,
    requirements: JobRequirements,
    *,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Objective SQL filtering, then a skill-overlap pre-rank."""
    stmt = sa.select(candidates).where(candidates.c.workspace_id == workspace_id)
    if requirements.min_experience_years:
        # keep a little headroom: adjacent experience can still be a fit
        stmt = stmt.where(
            sa.or_(
                candidates.c.experience_years.is_(None),
                candidates.c.experience_years >= max(0.0, requirements.min_experience_years - 1.0),
            )
        )
    stmt = stmt.limit(400)
    rows = [dict(r) for r in (await session.execute(stmt)).mappings().all()]
    if not rows:
        return []

    wanted = [normalize_skill(s) for s in requirements.required_skills + requirements.preferred_skills]
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        haystack = " ".join(
            str(item)
            for item in (
                (row.get("skills") or [])
                + (row.get("frameworks") or [])
                + (row.get("tools") or [])
                + (row.get("programming_languages") or [])
                + [row.get("current_role") or "", row.get("summary") or ""]
            )
        )
        hay_tokens = _tokens(haystack + " " + json.dumps(row.get("projects") or [], default=str))
        hits = sum(1 for skill in wanted if _contains(hay_tokens, haystack.lower(), skill))
        coverage = hits / len(wanted) if wanted else 0.5
        scored.append((coverage, row))

    scored.sort(key=lambda pair: (pair[0], pair[1].get("created_at") or utcnow()), reverse=True)
    return [row for _, row in scored[:limit]]


# -------------------------------------------------------------------- scoring
def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def score_candidate(
    candidate: dict[str, Any],
    requirements: JobRequirements,
    verification: dict[str, Any],
) -> dict[str, Any]:
    candidate_text = " ".join(
        str(item)
        for item in (
            (candidate.get("skills") or [])
            + (candidate.get("frameworks") or [])
            + (candidate.get("tools") or [])
            + (candidate.get("programming_languages") or [])
            + (candidate.get("previous_companies") or [])
        )
    ).lower()
    project_text = " ".join(
        f"{p.get('name') or ''} {p.get('description') or ''} {' '.join(p.get('technologies') or [])}"
        for p in (candidate.get("projects") or [])
    ).lower()
    tokens = _tokens(candidate_text + " " + project_text + " " + (candidate.get("summary") or ""))

    required = requirements.required_skills or []
    preferred = requirements.preferred_skills or []
    required_hits = [s for s in required if _contains(tokens, candidate_text, s)]
    preferred_hits = [s for s in preferred if _contains(tokens, candidate_text + " " + project_text, s)]
    missing = [s for s in required if s not in required_hits]

    if required:
        skills_score = 85.0 * (len(required_hits) / len(required))
        if preferred:
            skills_score += 15.0 * (len(preferred_hits) / len(preferred))
    else:
        skills_score = 60.0 if candidate.get("skills") else 35.0

    years = candidate.get("experience_years")
    min_years = requirements.min_experience_years
    if min_years and min_years > 0:
        if years is None:
            experience_score = 35.0
        else:
            ratio = years / min_years
            experience_score = 100.0 if ratio >= 1.25 else 85.0 if ratio >= 1.0 else 60.0 if ratio >= 0.6 else 25.0
    elif years is None:
        experience_score = 45.0
    else:
        experience_score = min(100.0, 40.0 + years * 10.0)

    project_terms = [t for t in (requirements.required_projects + requirements.responsibilities + required) if t]
    project_tokens: set[str] = set()
    for term in project_terms:
        project_tokens |= _tokens(term)
    if project_tokens:
        project_hits = len(project_tokens & _tokens(project_text))
        project_score = min(100.0, 100.0 * project_hits / max(6, len(project_tokens) ** 0.5 * 2))
    else:
        project_score = 65.0 if candidate.get("projects") else 40.0
    if candidate.get("projects"):
        project_score = max(project_score, 35.0)

    location_score = 70.0
    if requirements.required_location:
        wanted = requirements.required_location.lower()
        have = (candidate.get("location") or "").lower()
        preferred_loc = (candidate.get("preferred_location") or "").lower()
        if "remote" in wanted or "remote" in have:
            location_score = 100.0
        elif have and any(part.strip() in have for part in re.split(r"[,/|]", wanted) if part.strip()):
            location_score = 100.0
        elif preferred_loc and any(part.strip() in preferred_loc for part in re.split(r"[,/|]", wanted) if part.strip()):
            location_score = 85.0
        else:
            location_score = 40.0

    evidence_score = float(verification.get("evidence_score") or 0.0)

    weights = {**DEFAULT_WEIGHTS, **(requirements.weights or {})}
    total_weight = sum(weights.get(k, 0) for k in ("skills", "experience", "projects", "location", "evidence")) or 1.0
    total = (
        skills_score * weights.get("skills", 0)
        + experience_score * weights.get("experience", 0)
        + project_score * weights.get("projects", 0)
        + location_score * weights.get("location", 0)
        + evidence_score * weights.get("evidence", 0)
    ) / total_weight

    strengths: list[str] = []
    if required_hits:
        strengths.append(f"Matches {len(required_hits)}/{len(required)} required skills: " + ", ".join(required_hits[:8]))
    if preferred_hits:
        strengths.append("Also has preferred skills: " + ", ".join(preferred_hits[:6]))
    if years is not None:
        strengths.append(f"{years:g} years of experience" + (f" (role needs {min_years:g}+)" if min_years else ""))
    gh = verification.get("github") or {}
    if gh.get("available") and gh.get("relevant_repositories"):
        names = ", ".join(r["name"] for r in gh["relevant_repositories"][:3] if r.get("name"))
        strengths.append(f"Public GitHub repos reference required tech: {names}")
    lc = verification.get("leetcode") or {}
    if lc.get("available") and lc.get("total_solved"):
        strengths.append(f"LeetCode: {lc['total_solved']} problems solved")

    missing_requirements = [f"Missing required skill: {skill}" for skill in missing]
    if min_years and (years is None or years < min_years):
        missing_requirements.append(
            f"Experience below requirement ({'unknown' if years is None else f'{years:g} yrs'} vs {min_years:g} yrs)"
        )
    if location_score <= 40:
        missing_requirements.append(f"Location mismatch (role: {requirements.required_location})")
    if requirements.education and not (candidate.get("education") or candidate.get("university")):
        missing_requirements.append("Education not stated on the resume")

    concerns: list[str] = []
    if not gh.get("available"):
        concerns.append(f"GitHub not verified: {gh.get('reason')}")
    if (candidate.get("projects") or []) == []:
        concerns.append("No projects listed on the resume")
    if candidate.get("experience_years") is None:
        concerns.append("Experience could not be derived from stated employment dates")
    if not verification.get("signals"):
        concerns.append("No public verification signals available")

    explanation_parts = [
        f"{candidate.get('full_name') or 'Candidate'} scores {total:.0f}/100 for this role.",
        f"Skills {skills_score:.0f} ({len(required_hits)}/{len(required) or 0} required"
        + (f", {len(preferred_hits)} preferred" if preferred else "")
        + ").",
    ]
    if years is not None:
        explanation_parts.append(f"Experience {experience_score:.0f} ({years:g} yrs).")
    explanation_parts.append(f"Project relevance {project_score:.0f}, location {location_score:.0f}.")
    if verification.get("signals"):
        explanation_parts.append("Verification: " + "; ".join(verification["signals"][:3]) + ".")

    return {
        "match_score": round(_clamp(total), 1),
        "skills_score": round(_clamp(skills_score), 1),
        "experience_score": round(_clamp(experience_score), 1),
        "project_score": round(_clamp(project_score), 1),
        "location_score": round(_clamp(location_score), 1),
        "evidence_score": round(_clamp(evidence_score), 1),
        "weights": weights,
        "strengths": strengths,
        "missing_requirements": missing_requirements,
        "concerns": concerns,
        "explanation": " ".join(explanation_parts),
        "matched_required_skills": required_hits,
        "matched_preferred_skills": preferred_hits,
    }


_EVAL_SYSTEM = (
    "You are an expert AI recruiter evaluating a candidate against job requirements. "
    "Evaluate the candidate objectively on 5 dimensions (0-100 each):\n"
    "- skills_score: match with required and preferred skills\n"
    "- experience_score: candidate years of experience and seniority vs requirement\n"
    "- project_score: relevance, depth and complexity of actual project work vs role\n"
    "- location_score: location match or remote compatibility\n"
    "- evidence_score: verified public signals from GitHub/LeetCode\n\n"
    "Return JSON: {\n"
    '  "skills_score": number,\n'
    '  "experience_score": number,\n'
    '  "project_score": number,\n'
    '  "location_score": number,\n'
    '  "evidence_score": number,\n'
    '  "explanation": string,\n'
    '  "strengths": string[],\n'
    '  "missing_requirements": string[],\n'
    '  "concerns": string[],\n'
    '  "matched_required_skills": string[],\n'
    '  "matched_preferred_skills": string[]\n'
    "}"
)


async def evaluate_candidate_llm(
    candidate: dict[str, Any],
    requirements: JobRequirements,
    verification: dict[str, Any],
) -> dict[str, Any]:
    weights = {**DEFAULT_WEIGHTS, **(requirements.weights or {})}
    profile = {
        "name": candidate.get("full_name"),
        "current_role": candidate.get("current_role"),
        "experience_years": candidate.get("experience_years"),
        "location": candidate.get("location"),
        "skills": candidate.get("skills") or [],
        "projects": candidate.get("projects") or [],
        "education": candidate.get("education") or candidate.get("university"),
        "verification": verification,
    }
    prompt = (
        f"Job Requirements:\n{requirements.model_dump_json()}\n\n"
        f"Candidate Profile:\n{json.dumps(profile, default=str)}\n\n"
        "Evaluate the candidate now."
    )
    res = await llm.complete_json(system=_EVAL_SYSTEM, user=prompt, max_tokens=1500)
    skills_s = float(res.get("skills_score", 50))
    exp_s = float(res.get("experience_score", 50))
    proj_s = float(res.get("project_score", 50))
    loc_s = float(res.get("location_score", 50))
    evid_s = float(res.get("evidence_score", 0))

    total_weight = sum(weights.get(k, 0) for k in ("skills", "experience", "projects", "location", "evidence")) or 1.0
    match_s = (
        skills_s * weights.get("skills", 0)
        + exp_s * weights.get("experience", 0)
        + proj_s * weights.get("projects", 0)
        + loc_s * weights.get("location", 0)
        + evid_s * weights.get("evidence", 0)
    ) / total_weight

    return {
        "match_score": round(_clamp(match_s), 1),
        "skills_score": round(_clamp(skills_s), 1),
        "experience_score": round(_clamp(exp_s), 1),
        "project_score": round(_clamp(proj_s), 1),
        "location_score": round(_clamp(loc_s), 1),
        "evidence_score": round(_clamp(evid_s), 1),
        "weights": weights,
        "strengths": [str(s)[:300] for s in res.get("strengths", [])][:10],
        "missing_requirements": [str(m)[:300] for m in res.get("missing_requirements", [])][:10],
        "concerns": [str(c)[:300] for c in res.get("concerns", [])][:10],
        "explanation": str(res.get("explanation", ""))[:1500],
        "matched_required_skills": res.get("matched_required_skills", []),
        "matched_preferred_skills": res.get("matched_preferred_skills", []),
        "method": "llm",
    }


def _input_hash(requirements: JobRequirements, candidate: dict[str, Any], verification: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "requirements": requirements.model_dump(mode="json"),
            "candidate_updated": str(candidate.get("updated_at")),
            "verified_at": verification.get("verified_at"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()

