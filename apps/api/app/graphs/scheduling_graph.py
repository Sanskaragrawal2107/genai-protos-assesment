"""Interview scheduling pipeline (availability → approval → Teams/Meet → Gmail invite).

The flow is a short, strictly linear sequence with no branching that would
benefit from graph orchestration, so it is a plain async function rather than a
LangGraph ``StateGraph``. Nothing external is created unless the recruiter
approves: :func:`run_scheduling` mirrors ``services.interviews.propose`` which
queues a *pending action* when ``request.approve`` is false.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..composio import IntegrationError
from ..repo import get_candidate, meeting_provider_for
from ..schemas import ScheduleRequest
from ..services import interviews as svc

log = logging.getLogger("recruiter.graph.scheduling")


async def run_scheduling(
    *,
    session: AsyncSession,
    workspace_id: str,
    recruiter_id: str | None,
    candidate_id: str,
    request: ScheduleRequest,
) -> dict[str, Any]:
    """Optimized direct interview scheduling pipeline with zero redundant database queries."""
    candidate = await get_candidate(session, workspace_id, candidate_id)
    if not candidate:
        return {"status": "failed", "error": "Candidate not found"}

    if request.start is None:
        return {"status": "failed", "error": "A start time is required to schedule an interview"}

    provider = request.provider or await meeting_provider_for(session, workspace_id)
    tz_name = request.timezone or "Asia/Kolkata"

    try:
        check = await svc.availability(
            session,
            workspace_id=workspace_id,
            start=request.start,
            duration_minutes=request.duration_minutes,
            timezone_name=tz_name,
        )
    except IntegrationError as exc:
        log.warning("scheduling availability check failed: %s", exc)
        return {"status": "failed", "error": str(exc), "provider": str(exc)}

    if not check.get("available"):
        return {
            "status": "needs_new_slot",
            "reason": "That time conflicts with an existing event.",
            "conflicts": check.get("conflicts") or [],
            "alternatives": check.get("alternatives") or [],
            "timezone": tz_name,
            "calendar_connected": check.get("calendar_connected"),
        }

    req_with_provider = request.model_copy(update={"provider": provider, "timezone": tz_name})
    try:
        result = await svc.propose(
            session,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            candidate=candidate,
            request=req_with_provider,
        )
    except IntegrationError as exc:
        log.warning("scheduling propose failed: %s", exc)
        return {"status": "failed", "error": str(exc), "provider": str(exc)}

    return result or {"status": "failed", "error": "scheduling produced no result"}
