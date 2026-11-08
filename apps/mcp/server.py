"""AI Recruiter OS — FastMCP server.

Exposes the complete recruiting database plus the AI workflows as MCP tools so
an LLM client (Claude Desktop, Cursor, VS Code, n8n) can query and operate the
product directly. Every tool is a thin, typed wrapper around the same service
functions used by the FastAPI app — no duplicated business logic.

Run:
    python server.py            # stdio transport
    python server.py --http     # streamable HTTP on http://127.0.0.1:8765/mcp

Workspace resolution is server-side only (MCP_WORKSPACE_ID or MCP_USER_ID),
never taken from model input. Tools with external side effects require
confirm=true and are written to the approval + audit tables.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

API_DIR = Path(__file__).resolve().parent.parent / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from fastmcp import FastMCP  # noqa: E402

import sqlalchemy as sa  # noqa: E402

from app import db as db_module  # noqa: E402
from app.composio import composio  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session  # noqa: E402
from app.graphs.assistant_graph import run_assistant  # noqa: E402
from app.graphs.evaluation_graph import run_evaluation  # noqa: E402
from app.graphs.scheduling_graph import run_scheduling  # noqa: E402
from app.repo import (  # noqa: E402
    candidate_detail,
    create_pending_action,
    get_candidate,
    list_candidates,
    list_pending_actions,
    log_activity,
    meeting_provider_for,
    set_candidate_status,
    to_jsonable,
)
from app.schemas import JobRequirements, ScheduleRequest  # noqa: E402
from app.services import assistant as assistant_svc  # noqa: E402
from app.services import communications as comms  # noqa: E402
from app.services import ingestion as ing  # noqa: E402
from app.services import interviews as interview_svc  # noqa: E402
from app.services.dashboard import dashboard_stats  # noqa: E402
from app.services.evaluation import score_candidate  # noqa: E402
from app.services.verification import verify_candidate as run_verification  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("recruiter.mcp")

mcp = FastMCP(
    "AI Recruiter OS",
    instructions=(
        "Recruiting operating system. Use find_candidates/ask_recruiter_assistant for "
        "natural-language queries and evaluate_candidates to rank candidates against a "
        "job description. Any external side effect (sending an email, creating or "
        "cancelling an interview) must be confirmed by a human: call the tool without "
        "confirm to create a pending action, then approve it after the recruiter agrees."
    ),
)

_ready = False
_ready_lock = asyncio.Lock()


def _j(value: Any) -> Any:
    return to_jsonable(value)


async def _ensure_db() -> None:
    global _ready
    if _ready:
        return
    async with _ready_lock:
        if _ready:
            return
        settings.validate_runtime()
        await init_db()
        _ready = True


async def _workspace(db) -> str:
    if settings.mcp_workspace_id:
        row = (
            await db.execute(
                sa.select(db_module.workspaces.c.id).where(
                    db_module.workspaces.c.id == settings.mcp_workspace_id
                )
            )
        ).scalar_one_or_none()
        if row:
            return row
        raise RuntimeError(f"MCP_WORKSPACE_ID {settings.mcp_workspace_id} does not exist")

    if not settings.mcp_user_id:
        raise RuntimeError("MCP_WORKSPACE_ID or MCP_USER_ID must be set")

    from app.repo import ensure_workspace
    from app.security import Identity

    identity = Identity(
        user_id=settings.mcp_user_id, email=None, name="MCP operator", mode="mcp"
    )
    return await ensure_workspace(db, identity)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _require_confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise ValueError(
            f"{what} requires confirm=true. Ask the recruiter for explicit approval first."
        )


# ------------------------------------------------------------------ overview
@mcp.tool
async def workspace_overview() -> dict[str, Any]:
    """Dashboard counters, recent ingestion jobs, recent activity and upcoming interviews."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        stats = await dashboard_stats(db, ws)
        return _j({"workspace_id": ws, **stats})


@mcp.tool
async def list_candidates(
    status: str | None = None,
    location: str | None = None,
    skills: list[str] | None = None,
    min_experience_years: float | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """List candidates using database filters (status, location, skills, experience)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        rows = await list_candidates(
            db,
            ws,
            filters={
                "pipeline_status": [status] if status else [],
                "location": location,
                "skills": skills or [],
                "min_experience_years": min_experience_years,
            },
            limit=min(max(limit, 1), 100),
        )
        return _j(rows)


@mcp.tool
async def get_candidate(candidate_id: str) -> dict[str, Any]:
    """Full candidate profile: extraction, projects, communications, interviews, evaluations."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        detail = await candidate_detail(db, ws, candidate_id)
        if not detail:
            raise ValueError("Candidate not found")
        return _j(detail)


@mcp.tool
async def ask_recruiter_assistant(message: str) -> dict[str, Any]:
    """Run the conversational recruiter workflow (search, evaluate, compare, schedule, email drafts)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        thread_id = db_module.new_id()
        now = db_module.utcnow()
        await db.execute(
            sa.insert(db_module.chat_threads).values(
                id=thread_id,
                workspace_id=ws,
                user_id=settings.mcp_user_id,
                title=message[:80],
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()
        result = await run_assistant(
            session=db,
            workspace_id=ws,
            recruiter_id=settings.mcp_user_id,
            thread_id=thread_id,
            user_message=message,
        )
        return _j(result)


# ----------------------------------------------------------------- pipeline
@mcp.tool
async def update_candidate_status(candidate_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
    """Move a candidate through the pipeline. Valid: APPLIED, SCREENED, SHORTLISTED, CONTACTED,
    INTERVIEW_SCHEDULED, INTERVIEWED, SELECTED, REJECTED, ON_HOLD."""
    allowed = {
        "APPLIED", "SCREENED", "SHORTLISTED", "CONTACTED", "INTERVIEW_SCHEDULED",
        "INTERVIEWED", "SELECTED", "REJECTED", "ON_HOLD",
    }
    if status not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        previous = await set_candidate_status(
            db, workspace_id=ws, candidate_id=candidate_id, to_status=status,
            changed_by=settings.mcp_user_id, reason=reason,
        )
        if previous is None:
            raise ValueError("Candidate not found")
        await log_activity(
            db, workspace_id=ws, recruiter_id=settings.mcp_user_id,
            action="candidate.status_changed", entity_type="candidate", entity_id=candidate_id,
            metadata={"from": previous, "to": status, "reason": reason, "via": "mcp"},
        )
        await db.commit()
        return _j({"candidate_id": candidate_id, "from": previous, "to": status})


@mcp.tool
async def add_candidate_note(candidate_id: str, note: str, next_followup_iso: str | None = None) -> dict[str, Any]:
    """Add a recruiter note (optionally with a next follow-up date)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        candidate = await get_candidate(db, ws, candidate_id)
        if not candidate:
            raise ValueError("Candidate not found")
        note_id = db_module.new_id()
        await db.execute(
            sa.insert(db_module.recruiter_notes).values(
                id=note_id,
                workspace_id=ws,
                candidate_id=candidate_id,
                recruiter_id=settings.mcp_user_id,
                note=note,
                next_followup_at=_parse_iso(next_followup_iso),
                created_at=db_module.utcnow(),
            )
        )
        await db.commit()
        return _j({"note_id": note_id, "candidate_id": candidate_id})


# --------------------------------------------------------------------- jobs
@mcp.tool
async def list_jobs() -> list[dict[str, Any]]:
    """List job descriptions with their evaluation counts."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        rows = (
            await db.execute(
                sa.select(db_module.jobs)
                .where(db_module.jobs.c.workspace_id == ws)
                .order_by(db_module.jobs.c.created_at.desc())
            )
        ).mappings().all()
        return _j([dict(row) for row in rows])


@mcp.tool
async def create_job(title: str, description: str) -> dict[str, Any]:
    """Create a job description to evaluate candidates against."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        job_id = db_module.new_id()
        now = db_module.utcnow()
        await db.execute(
            sa.insert(db_module.jobs).values(
                id=job_id, workspace_id=ws, title=title[:200], description=description,
                structured_requirements={}, status="open", created_by=settings.mcp_user_id,
                created_at=now, updated_at=now,
            )
        )
        await db.commit()
        return _j({"id": job_id, "title": title, "status": "open"})


@mcp.tool
async def evaluate_candidates(job_id: str, top: int = 10, verify: bool = True) -> dict[str, Any]:
    """Structure the JD, filter candidates in SQL, verify GitHub/LeetCode, score and rank them."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        job = (
            await db.execute(
                sa.select(db_module.jobs).where(
                    db_module.jobs.c.id == job_id, db_module.jobs.c.workspace_id == ws
                )
            )
        ).mappings().first()
        if not job:
            raise ValueError("Job not found")
        result = await run_evaluation(
            session=db, workspace_id=ws, job_id=job_id,
            description=job["description"], title=job["title"], limit=25, verify=verify,
        )
        requirements = result.get("requirements")
        if requirements is not None:
            await db.execute(
                sa.update(db_module.jobs)
                .where(db_module.jobs.c.id == job_id)
                .values(structured_requirements=requirements.model_dump(mode="json"),
                        updated_at=db_module.utcnow())
            )
            await db.commit()
        return _j(
            {
                "job_id": job_id,
                "requirements": requirements.model_dump(mode="json") if requirements else None,
                "requirement_method": result.get("requirement_method"),
                "evaluated": len(result["ranked"]),
                "results": result["ranked"][: max(1, min(top, 50))],
            }
        )


@mcp.tool
async def compare_candidates(job_id: str, candidate_ids: list[str] | None = None, top: int = 5) -> dict[str, Any]:
    """Side-by-side score breakdown for the strongest candidates of a job."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        rows = (
            await db.execute(
                sa.select(db_module.candidate_evaluations, db_module.candidates)
                .join(db_module.candidates, db_module.candidates.c.id == db_module.candidate_evaluations.c.candidate_id)
                .where(
                    db_module.candidate_evaluations.c.job_id == job_id,
                    db_module.candidate_evaluations.c.workspace_id == ws,
                )
                .order_by(db_module.candidate_evaluations.c.match_score.desc())
            )
        ).all()
        selected = []
        for evaluation, candidate in rows:
            if candidate_ids and evaluation["candidate_id"] not in candidate_ids:
                continue
            selected.append(
                {
                    "candidate_id": evaluation["candidate_id"],
                    "name": candidate["full_name"],
                    "match_score": evaluation["match_score"],
                    "skills_score": evaluation["skills_score"],
                    "experience_score": evaluation["experience_score"],
                    "project_score": evaluation["project_score"],
                    "location_score": evaluation["location_score"],
                    "evidence_score": evaluation["evidence_score"],
                    "strengths": evaluation["strengths"],
                    "missing_requirements": evaluation["missing_requirements"],
                    "verification": evaluation["verification_evidence"],
                }
            )
            if len(selected) >= max(1, min(top, 25)):
                break
        return _j({"job_id": job_id, "candidates": selected})


@mcp.tool
async def verify_candidate(candidate_id: str, keywords: list[str] | None = None) -> dict[str, Any]:
    """Fetch public GitHub + LeetCode evidence for a candidate (never invents results)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        candidate = await get_candidate(db, ws, candidate_id)
        if not candidate:
            raise ValueError("Candidate not found")
        evidence = await run_verification(
            candidate, keywords=keywords or candidate.get("skills") or []
        )
        return _j(evidence)


# --------------------------------------------------------------------- sync
@mcp.tool
async def get_sync_status() -> dict[str, Any]:
    """Gmail connection state, sync schedule, failures and recent jobs."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        account = (
            await db.execute(
                sa.select(db_module.connected_accounts).where(
                    db_module.connected_accounts.c.workspace_id == ws,
                    db_module.connected_accounts.c.provider == "gmail",
                )
            )
        ).mappings().first()
        if not account:
            return _j({"connected": False})
        sync = (
            await db.execute(
                sa.select(db_module.inbox_sync_settings).where(
                    db_module.inbox_sync_settings.c.connected_account_id == account["id"]
                )
            )
        ).mappings().first()
        jobs = (
            await db.execute(
                sa.select(db_module.ingestion_jobs)
                .where(db_module.ingestion_jobs.c.workspace_id == ws)
                .order_by(db_module.ingestion_jobs.c.created_at.desc())
                .limit(10)
            )
        ).mappings().all()
        return _j(
            {
                "connected": True,
                "account": {
                    "id": account["id"],
                    "email": account["connected_email"],
                    "status": account["status"],
                },
                "settings": dict(sync) if sync else None,
                "recent_jobs": [dict(job) for job in jobs],
            }
        )


@mcp.tool
async def sync_inbox_now() -> dict[str, Any]:
    """Run a Gmail ingestion pass immediately (idempotent; dedupe is enforced in SQL)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        account = (
            await db.execute(
                sa.select(db_module.connected_accounts).where(
                    db_module.connected_accounts.c.workspace_id == ws,
                    db_module.connected_accounts.c.provider == "gmail",
                    db_module.connected_accounts.c.status == "active",
                )
            )
        ).mappings().first()
        if not account:
            raise ValueError("No active Gmail connection in this workspace")
        job = await ing.enqueue_job(db, workspace_id=ws, account_id=account["id"], trigger="manual")
        result = await ing.run_job(db, job=job, worker_id="mcp")
        return _j(result)


# --------------------------------------------------------------- interviews
@mcp.tool
async def list_interviews(limit: int = 50) -> list[dict[str, Any]]:
    """Scheduled and past interviews with meeting links and status."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(await interview_svc.upcoming_interviews(db, ws, limit=min(max(limit, 1), 200)))


@mcp.tool
async def check_availability(
    start_iso: str, duration_minutes: int = 30, timezone_name: str = "Asia/Kolkata"
) -> dict[str, Any]:
    """Check workspace-local interview conflicts and suggest free slots.

    Only interviews created by this product are considered. External calendar
    availability is never assumed or fabricated."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(
            await interview_svc.availability(
                db, workspace_id=ws, start=_parse_iso(start_iso),
                duration_minutes=duration_minutes, timezone_name=timezone_name,
            )
        )


@mcp.tool
async def schedule_interview(
    candidate_id: str,
    start_iso: str,
    duration_minutes: int = 30,
    timezone_name: str = "Asia/Kolkata",
    provider: Literal["microsoft_teams", "google_meet", "none"] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create the real meeting (Teams or Google Meet) and email the Gmail invitation.

    The provider defaults to the workspace setting and can be overridden per
    interview. Without confirm=true this only creates a pending action for the
    recruiter."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        candidate = await get_candidate(db, ws, candidate_id)
        if not candidate:
            raise ValueError("Candidate not found")
        resolved_provider = provider or await meeting_provider_for(db, ws)
        request = ScheduleRequest(
            candidate_id=candidate_id,
            start=_parse_iso(start_iso),
            duration_minutes=duration_minutes,
            timezone=timezone_name,
            provider=resolved_provider,
            title=f"Interview: {candidate['full_name']}",
            approve=confirm,
        )
        return _j(
            await run_scheduling(
                session=db, workspace_id=ws, recruiter_id=settings.mcp_user_id,
                candidate_id=candidate_id, request=request,
            )
        )


@mcp.tool
async def reschedule_interview(interview_id: str, new_start_iso: str, duration_minutes: int | None = None) -> dict[str, Any]:
    """Move an existing interview; the real meeting is updated and a
    Gmail update is queued for the attendees."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(
            await interview_svc.reschedule_interview(
                db, workspace_id=ws, interview_id=interview_id,
                new_start=_parse_iso(new_start_iso), recruiter_id=settings.mcp_user_id,
                duration_minutes=duration_minutes,
            )
        )


@mcp.tool
async def cancel_interview(interview_id: str, reason: str | None = None) -> dict[str, Any]:
    """Cancel an interview: delete the real meeting/calendar event and queue a
    Gmail cancellation notice."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(
            await interview_svc.cancel_interview(
                db, workspace_id=ws, interview_id=interview_id,
                recruiter_id=settings.mcp_user_id, reason=reason,
            )
        )


# ----------------------------------------------------------- communications
@mcp.tool
async def draft_email(
    candidate_id: str,
    kind: str = "follow_up",
    instructions: str | None = None,
) -> dict[str, Any]:
    """Draft a personalised email (LLM or template). Nothing is sent; it waits for approval."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        candidate = await get_candidate(db, ws, candidate_id)
        if not candidate:
            raise ValueError("Candidate not found")
        job = (
            await db.execute(
                sa.select(db_module.jobs)
                .where(db_module.jobs.c.workspace_id == ws, db_module.jobs.c.status == "open")
                .order_by(db_module.jobs.c.created_at.desc())
                .limit(1)
            )
        ).mappings().first()
        subject, body, method = await comms.draft_email(
            kind=kind, candidate=candidate, job_title=job["title"] if job else None,
            instructions=instructions,
        )
        comm = await comms.queue_email(
            db, workspace_id=ws, candidate=candidate, recruiter_id=settings.mcp_user_id,
            kind=kind, subject=subject, body=body, method=method,
        )
        action = await create_pending_action(
            db, workspace_id=ws, recruiter_id=settings.mcp_user_id, kind="send_email",
            title=f"Send {kind.replace('_', ' ')} to {candidate['full_name']}",
            payload={"communication_id": comm["id"], "candidate_id": candidate_id, "subject": subject},
        )
        return _j({"communication": comm, "pending_action": action, "method": method})


@mcp.tool
async def send_email(communication_id: str, confirm: bool = False) -> dict[str, Any]:
    """Send an already-drafted email through Gmail. Requires confirm=true."""
    _require_confirm(confirm, "Sending an email")
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(
            await comms.send_communication(
                db, workspace_id=ws, communication_id=communication_id,
                recruiter_id=settings.mcp_user_id,
            )
        )


@mcp.tool
async def list_pending_actions(status: str = "pending") -> list[dict[str, Any]]:
    """Approval queue (email sends, interview scheduling/cancellations)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(await list_pending_actions(db, ws, status=status))


@mcp.tool
async def approve_action(action_id: str) -> dict[str, Any]:
    """Execute a pending action (this performs the external side effect)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        return _j(
            await interview_svc.execute_pending_action(
                db, workspace_id=ws, action_id=action_id, recruiter_id=settings.mcp_user_id
            )
        )


@mcp.tool
async def reject_action(action_id: str, reason: str | None = None) -> dict[str, Any]:
    """Reject a pending action without executing it."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        action = await db.execute(
            sa.select(db_module.pending_actions).where(
                db_module.pending_actions.c.id == action_id,
                db_module.pending_actions.c.workspace_id == ws,
            )
        )
        row = action.mappings().first()
        if not row:
            raise ValueError("Action not found")
        await db.execute(
            sa.update(db_module.pending_actions)
            .where(db_module.pending_actions.c.id == action_id)
            .values(status="rejected", executed_at=db_module.utcnow(), error_message=reason)
        )
        payload = row["payload"] or {}
        if row["kind"] == "send_email" and payload.get("communication_id"):
            await db.execute(
                sa.update(db_module.candidate_communications)
                .where(db_module.candidate_communications.c.id == payload["communication_id"])
                .values(status="rejected")
            )
        await db.commit()
        return _j({"status": "rejected", "id": action_id})


# ------------------------------------------------------------- composio / MCP
@mcp.tool
async def composio_status() -> dict[str, Any]:
    """Report whether the Composio tool layer is configured for this workspace.

    Never fabricates availability: when Composio is not configured it says so
    and explains which environment variables are missing."""
    from app.services.composio_mcp import TOOLKITS, composio_user_id

    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
    if not settings.composio_enabled:
        return {
            "configured": False,
            "toolkits": list(TOOLKITS),
            "user_id": composio_user_id(ws),
            "error": (
                "Composio is not configured. Set COMPOSIO_API_KEY; auth configs "
                "for Gmail, Microsoft Teams and Google Meet are created automatically."
            ),
        }
    accounts: list[dict[str, Any]] = []
    try:
        accounts = await composio.list_connected_accounts(
            user_id=composio_user_id(ws)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Composio connected-account lookup failed: %s", exc)
    return {
        "configured": True,
        "toolkits": list(TOOLKITS),
        "user_id": composio_user_id(ws),
        "accounts": [_j(a) for a in accounts],
    }


@mcp.tool
async def composio_session() -> dict[str, Any]:
    """Create/reuse the workspace's hosted Composio MCP session (Gmail + Teams + Google).

    Returns a browser-safe summary only; the MCP URL and its auth headers stay
    on the server. When it is mounted, these tools are reachable under the
    ``composio`` namespace of this very server."""
    from app.services.composio_mcp import public_view, session_for_workspace

    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
    try:
        mcp_session = await session_for_workspace(ws)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Composio MCP session unavailable: {exc}") from exc
    return _j(public_view(mcp_session))


async def _mount_composio_proxy() -> bool:
    """Mount Composio's hosted MCP (Gmail + Teams + Google) under ``composio/``.

    The bridge is optional: when Composio is unconfigured, no workspace is
    fixed, or fastmcp lacks proxy support, we log and mount nothing rather than
    exposing broken tools. Credentials are held server-side here."""
    if not settings.composio_enabled:
        log.info("Composio not configured — hosted-MCP proxy not mounted")
        return False
    if not settings.mcp_workspace_id:
        log.info("MCP_WORKSPACE_ID not set — hosted-MCP proxy not mounted")
        return False
    try:
        from fastmcp.client.transports import StreamableHttpTransport
        from fastmcp.server import create_proxy

        from app.services.composio_mcp import session_for_workspace
    except Exception as exc:  # noqa: BLE001
        log.warning("fastmcp proxy support unavailable: %s", exc)
        return False
    try:
        workspace_session = await session_for_workspace(settings.mcp_workspace_id)
        transport = StreamableHttpTransport(
            url=workspace_session.url, headers=workspace_session.headers
        )
        proxy = create_proxy(transport, name="Composio (Gmail + Teams + Google)")
        try:
            mcp.mount(proxy, namespace="composio")
        except TypeError:
            mcp.mount(proxy, prefix="composio")
        log.info(
            "Mounted Composio hosted MCP at namespace 'composio' (%s)",
            ", ".join(workspace_session.toolkits),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not mount Composio hosted-MCP proxy: %s", exc)
        return False


# ---------------------------------------------------------------- resources
@mcp.resource("recruiter://overview")
async def overview_resource() -> str:
    """Live pipeline summary as JSON (for context injection)."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        stats = await dashboard_stats(db, ws)
        return json.dumps(_j({"workspace_id": ws, **stats}), indent=2, default=str)


@mcp.resource("recruiter://candidates/{candidate_id}")
async def candidate_resource(candidate_id: str) -> str:
    """A single candidate profile as JSON."""
    await _ensure_db()
    async with session() as db:
        ws = await _workspace(db)
        detail = await candidate_detail(db, ws, candidate_id)
        if not detail:
            raise ValueError("Candidate not found")
        return json.dumps(_j(detail), indent=2, default=str)


@mcp.prompt
def weekly_pipeline_review(job_title: str = "the open role") -> str:
    """Prompt template for a weekly recruiting review."""
    return (
        f"Review the pipeline for {job_title}. Call workspace_overview, then list_jobs and "
        "evaluate_candidates for the matching job. Summarise: strongest three candidates with "
        "evidence, candidates who have not been contacted, interviews scheduled this week, and "
        "which approvals are waiting. Propose follow-up emails but do not send anything without "
        "explicit confirmation."
    )


if __name__ == "__main__":
    try:
        asyncio.run(_mount_composio_proxy())
    except Exception as exc:  # noqa: BLE001
        log.warning("Composio proxy mount skipped: %s", exc)
    if "--http" in sys.argv:
        mcp.run(transport="http", host="127.0.0.1", port=8765)
    else:
        mcp.run()
