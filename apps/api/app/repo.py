"""Workspace-scoped data access helpers.

Every query in the API goes through this module (or explicitly filters on
``workspace_id``), which is the application-level mirror of the Postgres RLS
policies in ``supabase/migrations/0002_rls.sql``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from . import db
from .db import (
    candidate_communications,
    candidate_evaluations,
    candidate_status_history,
    candidates,
    connected_accounts,
    interviews,
    pending_actions,
    recruiter_activity_logs,
    recruiter_notes,
    workspace_members,
    workspaces,
)
from .security import Identity

log = logging.getLogger("recruiter.repo")

DEFAULT_WEIGHTS = {
    "skills": 0.38,
    "experience": 0.20,
    "projects": 0.22,
    "location": 0.08,
    "evidence": 0.12,
}


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    return value or None


_WORKSPACE_CACHE: dict[str, tuple[float, str]] = {}
_WORKSPACE_CACHE_TTL = 300.0


async def ensure_workspace(session: AsyncSession, identity: Identity) -> str:
    """Return the caller's workspace, creating a personal one on first login."""
    now_ts = time.time()
    if identity.user_id in _WORKSPACE_CACHE:
        cached_at, cached_ws_id = _WORKSPACE_CACHE[identity.user_id]
        if now_ts - cached_at < _WORKSPACE_CACHE_TTL:
            return cached_ws_id

    stmt = (
        sa.select(workspace_members.c.workspace_id)
        .where(workspace_members.c.user_id == identity.user_id)
        .order_by(workspace_members.c.created_at.asc())
        .limit(1)
    )
    ws_id = (await session.execute(stmt)).scalar_one_or_none()
    if ws_id:
        _WORKSPACE_CACHE[identity.user_id] = (now_ts, ws_id)
        return ws_id

    ws_id = db.new_id()
    now = db.utcnow()
    name = f"{identity.name or identity.email or 'Personal'} workspace"
    await session.execute(
        sa.insert(workspaces).values(
            id=ws_id,
            name=name,
            settings={"require_approval": True},
            created_at=now,
            updated_at=now,
        )
    )
    await session.execute(
        sa.insert(workspace_members).values(
            id=db.new_id(),
            workspace_id=ws_id,
            user_id=identity.user_id,
            email=identity.email,
            display_name=identity.name,
            role="owner",
            created_at=now,
        )
    )
    await session.commit()
    log.info("provisioned workspace %s for %s", ws_id, identity.user_id)
    return ws_id


async def get_workspace(session: AsyncSession, workspace_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(sa.select(workspaces).where(workspaces.c.id == workspace_id))
    ).mappings().first()
    return dict(row) if row else None


MEETING_PROVIDERS = {"microsoft_teams", "google_meet", "none"}

# Older workspace settings stored these values; map them onto the canonical id.
_PROVIDER_ALIASES = {
    "teams": "microsoft_teams",
    "google": "google_meet",
    "googlemeet": "google_meet",
    "meet": "google_meet",
}


def normalize_meeting_provider(provider: str | None) -> str:
    value = (provider or "").strip().lower()
    compact = value.replace(" ", "").replace("_", "").replace("-", "")
    value = _PROVIDER_ALIASES.get(value) or _PROVIDER_ALIASES.get(compact, value)
    return value if value in MEETING_PROVIDERS else "microsoft_teams"


async def connected_meeting_providers(session: AsyncSession, workspace_id: str) -> set[str]:
    """Video platforms the workspace can actually use right now.

    There is no workspace-level default meeting provider: the recruiter chooses
    the platform per meeting (in chat). This reads the connected accounts and
    returns canonical provider ids (``google_meet`` / ``microsoft_teams``).
    """
    rows = (
        await session.execute(
            sa.select(connected_accounts.c.provider).where(
                connected_accounts.c.workspace_id == workspace_id,
                connected_accounts.c.status == "active",
            )
        )
    ).scalars().all()
    connected = {str(provider) for provider in rows}
    platforms: set[str] = set()
    if connected & {"google_meet", "google_calendar"}:
        platforms.add("google_meet")
    if "microsoft_teams" in connected:
        platforms.add("microsoft_teams")
    return platforms


async def log_activity(
    session: AsyncSession,
    *,
    workspace_id: str,
    recruiter_id: str | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        sa.insert(recruiter_activity_logs).values(
            id=db.new_id(),
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata=to_jsonable(metadata or {}),
            created_at=db.utcnow(),
        )
    )


async def set_candidate_status(
    session: AsyncSession,
    *,
    workspace_id: str,
    candidate_id: str,
    to_status: str,
    changed_by: str | None,
    reason: str | None = None,
    commit: bool = True,
) -> str | None:
    row = (
        await session.execute(
            sa.select(candidates.c.pipeline_status).where(
                candidates.c.id == candidate_id, candidates.c.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row == to_status:
        return row
    await session.execute(
        sa.update(candidates)
        .where(candidates.c.id == candidate_id, candidates.c.workspace_id == workspace_id)
        .values(pipeline_status=to_status, updated_at=db.utcnow())
    )
    await session.execute(
        sa.insert(candidate_status_history).values(
            id=db.new_id(),
            workspace_id=workspace_id,
            candidate_id=candidate_id,
            from_status=row,
            to_status=to_status,
            changed_by=changed_by,
            reason=reason,
            created_at=db.utcnow(),
        )
    )
    if commit:
        await session.commit()
    return row


async def get_candidate(session: AsyncSession, workspace_id: str, candidate_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            sa.select(candidates).where(
                candidates.c.id == candidate_id, candidates.c.workspace_id == workspace_id
            )
        )
    ).mappings().first()
    return dict(row) if row else None


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def candidate_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "full_name": row.get("full_name") or "Unknown candidate",
        "email": row.get("email"),
        "phone": row.get("phone"),
        "location": row.get("location"),
        "experience_years": row.get("experience_years"),
        "current_role": row.get("current_role"),
        "current_company": row.get("current_company"),
        "skills": _json_list(row.get("skills"))[:24],
        "github_url": row.get("github_url"),
        "leetcode_url": row.get("leetcode_url"),
        "linkedin_url": row.get("linkedin_url"),
        "portfolio_url": row.get("portfolio_url"),
        "pipeline_status": row.get("pipeline_status") or "APPLIED",
        "resume_storage_path": row.get("resume_storage_path"),
        "created_at": row.get("created_at"),
        "match_score": row.get("match_score"),
        "evaluation": row.get("evaluation"),
    }


async def list_candidates(
    session: AsyncSession,
    workspace_id: str,
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filters = filters or {}
    stmt = sa.select(candidates).where(candidates.c.workspace_id == workspace_id)

    if filters.get("pipeline_status"):
        stmt = stmt.where(candidates.c.pipeline_status.in_(filters["pipeline_status"]))
    if filters.get("location"):
        variants = location_variants(str(filters["location"]))
        stmt = stmt.where(
            sa.or_(
                *[sa.func.lower(candidates.c.location).like(f"%{variant}%") for variant in variants]
            )
        )
    if filters.get("min_experience_years") is not None:
        stmt = stmt.where(candidates.c.experience_years >= filters["min_experience_years"])
    if filters.get("max_experience_years") is not None:
        stmt = stmt.where(candidates.c.experience_years <= filters["max_experience_years"])
    if filters.get("has_github"):
        stmt = stmt.where(candidates.c.github_url.isnot(None))
    if filters.get("not_contacted"):
        contacted = sa.select(candidate_communications.c.candidate_id).where(
            candidate_communications.c.workspace_id == workspace_id,
            candidate_communications.c.direction == "outbound",
            candidate_communications.c.status == "sent",
        )
        stmt = stmt.where(candidates.c.id.notin_(contacted))

    stmt = stmt.order_by(candidates.c.created_at.desc()).limit(min(limit, 200)).offset(offset)
    rows = (await session.execute(stmt)).mappings().all()
    results = [candidate_summary(dict(r)) for r in rows]

    skills = [s.lower() for s in filters.get("skills") or [] if s]
    if skills:
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in results:
            haystack = " ".join(
                str(s).lower()
                for s in (item.get("skills") or [])
                + [item.get("current_role") or ""]
            )
            hits = sum(1 for skill in skills if skill in haystack)
            if hits:
                scored.append((hits / len(skills), item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = [{**item, "match_score": round(score * 100, 1)} for score, item in scored]
    return results


async def candidate_detail(session: AsyncSession, workspace_id: str, candidate_id: str) -> dict[str, Any] | None:
    row = await get_candidate(session, workspace_id, candidate_id)
    if not row:
        return None
    detail = candidate_summary(row)
    detail.update(
        {
            "preferred_location": row.get("preferred_location"),
            "previous_companies": _json_list(row.get("previous_companies")),
            "education": _json_list(row.get("education")),
            "university": row.get("university"),
            "graduation_year": row.get("graduation_year"),
            "programming_languages": _json_list(row.get("programming_languages")),
            "frameworks": _json_list(row.get("frameworks")),
            "tools": _json_list(row.get("tools")),
            "projects": _json_list(row.get("projects")),
            "summary": row.get("summary"),
            "portfolio_url": row.get("portfolio_url"),
            "resume_filename": row.get("resume_filename"),
            "extraction_method": row.get("extraction_method"),
            "updated_at": row.get("updated_at"),
        }
    )

    async def _rows(table, order_col, limit: int = 100):
        stmt = (
            sa.select(table)
            .where(table.c.candidate_id == candidate_id)
            .order_by(order_col.desc())
            .limit(limit)
        )
        return [dict(r) for r in (await session.execute(stmt)).mappings().all()]

    applications = (
        await session.execute(
            sa.select(db.candidate_applications)
            .where(db.candidate_applications.c.candidate_id == candidate_id)
            .order_by(db.candidate_applications.c.created_at.desc())
            .limit(20)
        )
    ).mappings().all()
    detail["applications"] = [dict(r) for r in applications]
    detail["communications"] = await _rows(candidate_communications, candidate_communications.c.created_at)
    detail["interviews"] = await _rows(interviews, interviews.c.start_time)
    detail["status_history"] = await _rows(candidate_status_history, candidate_status_history.c.created_at)
    detail["notes"] = await _rows(recruiter_notes, recruiter_notes.c.created_at)
    detail["evaluations"] = (
        await session.execute(
            sa.select(candidate_evaluations)
            .where(candidate_evaluations.c.candidate_id == candidate_id)
            .order_by(candidate_evaluations.c.evaluated_at.desc())
            .limit(10)
        )
    ).mappings().all()
    detail["evaluations"] = [dict(r) for r in detail["evaluations"]]
    return detail


async def create_pending_action(
    session: AsyncSession,
    *,
    workspace_id: str,
    recruiter_id: str | None,
    kind: str,
    title: str,
    payload: dict[str, Any],
    expires_in_hours: int = 72,
) -> dict[str, Any]:
    action_id = db.new_id()
    now = db.utcnow()
    await session.execute(
        sa.insert(pending_actions).values(
            id=action_id,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            kind=kind,
            title=title,
            payload=to_jsonable(payload),
            status="pending",
            result={},
            created_at=now,
            expires_at=now + timedelta(hours=expires_in_hours),
        )
    )
    await session.commit()
    return {
        "id": action_id,
        "kind": kind,
        "title": title,
        "payload": payload,
        "status": "pending",
        "result": {},
        "created_at": now,
    }


async def list_pending_actions(
    session: AsyncSession, workspace_id: str, status: str = "pending", limit: int = 20
) -> list[dict[str, Any]]:
    stmt = (
        sa.select(pending_actions)
        .where(pending_actions.c.workspace_id == workspace_id, pending_actions.c.status == status)
        .order_by(pending_actions.c.created_at.desc())
        .limit(limit)
    )
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]


async def find_account(session: AsyncSession, workspace_id: str, provider: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            sa.select(connected_accounts)
            .where(
                connected_accounts.c.workspace_id == workspace_id,
                connected_accounts.c.provider == provider,
                connected_accounts.c.status == "active",
            )
            .order_by(connected_accounts.c.created_at.asc())
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


LOCATION_ALIASES: dict[str, list[str]] = {
    "bangalore": ["bangalore", "bengaluru", "blr"],
    "bengaluru": ["bangalore", "bengaluru", "blr"],
    "bombay": ["mumbai", "bombay"],
    "mumbai": ["mumbai", "bombay"],
    "gurgaon": ["gurgaon", "gurugram"],
    "new york": ["new york", "nyc", "new york city"],
    "remote": ["remote", "work from home", "wfh"],
}


def location_variants(value: str) -> list[str]:
    key = value.strip().lower()
    return LOCATION_ALIASES.get(key, [key])


def to_jsonable(value: Any) -> Any:
    """Make service payloads JSON-column safe (pydantic models, datetimes)."""
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value



