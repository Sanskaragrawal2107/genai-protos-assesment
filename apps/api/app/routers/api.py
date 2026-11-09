"""Product API (all routes workspace-scoped)."""
from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, AsyncIterator

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from .. import db as db_module
from ..composio import IntegrationError, composio, extract_email_from_account
from ..config import settings
from ..db import (
    candidate_communications,
    candidate_evaluations,
    candidates,
    chat_threads,
    connected_accounts,
    email_attachments,
    inbox_sync_settings,
    ingestion_jobs,
    interviews,
    jobs,
    pending_actions,
    recruiter_notes,
    utcnow,
)
from ..deps import CtxDep
from ..graphs.assistant_graph import run_assistant, run_assistant_stream
from ..graphs.evaluation_graph import run_evaluation
from ..graphs.scheduling_graph import run_scheduling
from ..repo import (
    candidate_detail,
    candidate_summary,
    create_pending_action,
    find_account,
    get_candidate,
    get_workspace_profile,
    list_candidates,
    list_pending_actions,
    log_activity,
    set_candidate_status,
    set_workspace_profile,
    workspace_profile,
)
from ..schemas import (
    CandidateFilters,
    ChatRequest,
    ConnectRequest,
    JobCreate,
    JobUpdate,
    NoteCreate,
    ScheduleRequest,
    SendEmailRequest,
    StatusUpdate,
    SyncSettingsIn,
    WorkspaceProfileIn,
)
from ..services import communications as comms
from ..services import composio_mcp
from ..services import ingestion as ing
from ..services.calendar import clear_provider_failure
from ..services.interviews import (
    availability,
    cancel_interview,
    execute_pending_action,
    reschedule_interview,
    upcoming_interviews,
)
from ..services.storage import storage

log = logging.getLogger("recruiter.api")
router = APIRouter(prefix="/api")

SYNC_PRESETS = [15, 30, 60, 180, 360, 1440]


# --------------------------------------------------------------------- core
@router.get("/me")
async def me(ctx: CtxDep) -> dict[str, Any]:
    connectors = (
        await ctx.session.execute(
            sa.select(connected_accounts)
            .where(connected_accounts.c.workspace_id == ctx.workspace_id)
            .order_by(
                (connected_accounts.c.status == "active").desc(),
                (connected_accounts.c.status != "revoked").desc(),
                connected_accounts.c.created_at.desc(),
            )
        )
    ).mappings().all()
    ws = (
        await ctx.session.execute(sa.select(db_module.workspaces).where(db_module.workspaces.c.id == ctx.workspace_id))
    ).mappings().first()
    profile = workspace_profile(dict(ws) if ws else None)
    return {
        "identity": {
            "user_id": ctx.identity.user_id,
            "email": ctx.identity.email,
            "name": ctx.identity.name,
            "mode": ctx.identity.mode,
        },
        "workspace": {
            "id": ctx.workspace_id,
            "name": ws["name"] if ws else "Workspace",
            **profile,
            # The onboarding prompt only needs a company name; phone/interviewer
            # can be completed later from Settings.
            "profile_complete": bool(profile.get("company_name")),
        },
        "capabilities": {
            "llm": settings.llm_enabled,
            "composio": settings.composio_enabled,
            "storage": storage.backend,
            "supabase_auth": bool(settings.supabase_jwt_secret),
        },
        "connectors": [_connector(row) for row in connectors],
    }


@router.put("/workspace/profile")
async def update_workspace_profile(ctx: CtxDep, body: WorkspaceProfileIn) -> dict[str, Any]:
    """Save the company name, phone and interviewer used in every email."""
    profile = await set_workspace_profile(
        ctx.session,
        ctx.workspace_id,
        company_name=body.company_name,
        company_phone=body.company_phone,
        interviewer_name=body.interviewer_name,
    )
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="workspace.profile_updated",
        entity_type="workspace",
        entity_id=ctx.workspace_id,
        metadata={"profile_complete": bool(profile.get("company_name"))},
    )
    await ctx.session.commit()
    return {
        "status": "saved",
        "profile_complete": bool(profile.get("company_name")),
        "profile": profile,
    }


@router.get("/team")
async def team(ctx: CtxDep) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(db_module.workspace_members)
            .where(db_module.workspace_members.c.workspace_id == ctx.workspace_id)
            .order_by(db_module.workspace_members.c.created_at.asc())
        )
    ).mappings().all()
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
            "is_you": row["user_id"] == ctx.identity.user_id,
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@router.get("/activity")
async def activity(ctx: CtxDep, limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(db_module.recruiter_activity_logs)
            .where(db_module.recruiter_activity_logs.c.workspace_id == ctx.workspace_id)
            .order_by(db_module.recruiter_activity_logs.c.created_at.desc())
            .limit(limit)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _connector(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "provider": data["provider"],
        "connected_email": data.get("connected_email"),
        "display_name": data.get("display_name"),
        "status": data["status"],
        "last_error": data.get("last_error"),
        "created_at": data.get("created_at"),
    }


# Connector ids are product-level names; Composio toolkits use their own slugs.
_TOOLKIT_FOR_PROVIDER = {
    "gmail": "gmail",
    "microsoft_teams": "microsoft_teams",
    "google_calendar": "googlecalendar",
    "google_meet": "googlemeet",
}
_PROVIDER_ALIASES = {
    "googlecalendar": "google_calendar",
    "googlemeet": "google_meet",
    "teams": "microsoft_teams",
}


def normalize_connector_provider(provider: str) -> str:
    value = (provider or "").strip().lower()
    value = _PROVIDER_ALIASES.get(value, value)
    if value not in _TOOLKIT_FOR_PROVIDER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "provider must be gmail | microsoft_teams | google_calendar | google_meet",
        )
    return value


# --------------------------------------------------------------- connectors
@router.get("/connectors")
async def connectors(ctx: CtxDep) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(connected_accounts)
            .where(connected_accounts.c.workspace_id == ctx.workspace_id)
            .order_by(
                (connected_accounts.c.status == "active").desc(),
                (connected_accounts.c.status != "revoked").desc(),
                connected_accounts.c.created_at.desc(),
            )
        )
    ).mappings().all()
    out = []
    gmail_ids = [row["id"] for row in rows if row["provider"] == "gmail"]
    sync_by_account: dict[str, Any] = {}
    if gmail_ids:
        sync_rows = (
            await ctx.session.execute(
                sa.select(inbox_sync_settings).where(
                    inbox_sync_settings.c.connected_account_id.in_(gmail_ids)
                )
            )
        ).mappings().all()
        sync_by_account = {r["connected_account_id"]: r for r in sync_rows}
    for row in rows:
        item = _connector(row)
        sync = sync_by_account.get(row["id"])
        if sync:
            item["sync"] = {
                "enabled": sync["enabled"],
                "interval_minutes": sync["interval_minutes"],
                "last_synced_at": sync["last_synced_at"],
                "next_sync_at": sync["next_sync_at"],
                "last_sync_status": sync["last_sync_status"],
                "last_sync_error": sync["last_sync_error"],
                "consecutive_failures": sync["consecutive_failures"],
            }
        out.append(item)
    return out


@router.get("/integrations/composio")
async def composio_integrations(ctx: CtxDep) -> dict[str, Any]:
    """Connection + hosted-MCP status for the chatbot integration."""
    user_id = composio_mcp.composio_user_id(ctx.workspace_id)
    result: dict[str, Any] = {
        "configured": settings.composio_enabled,
        "toolkits": list(composio_mcp.TOOLKITS),
        "user_id": user_id,
        "accounts": [],
        "mcp": {"available": False, "session": None},
        "error": None,
    }
    if not settings.composio_enabled:
        result["error"] = "COMPOSIO_API_KEY is not configured; Gmail, Microsoft Teams and Google Meet are unavailable."
        return result
    try:
        accounts = await composio.list_connected_accounts(user_id=user_id)
        result["accounts"] = [
            {
                "id": account.get("id"),
                "toolkit": (account.get("toolkit") or {}).get("slug")
                if isinstance(account.get("toolkit"), dict)
                else account.get("toolkit"),
                "status": account.get("status"),
                "alias": account.get("alias"),
            }
            for account in accounts
        ]
        result["mcp"]["available"] = True
    except IntegrationError as exc:
        result["error"] = str(exc)
    return result


@router.post("/integrations/composio/session")
async def composio_session(ctx: CtxDep) -> dict[str, Any]:
    """Provision the scoped hosted-MCP session used by the chatbot.

    Only a browser-safe view is returned. The FastMCP bridge in ``apps/mcp``
    creates the same session server-side, where it can use the auth headers.
    """
    if not settings.composio_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Composio is not configured. Set COMPOSIO_API_KEY to enable the MCP bridge.",
        )
    try:
        session = await composio_mcp.session_for_workspace(ctx.workspace_id)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Composio error: {exc}") from exc
    return {"status": "ready", "session": composio_mcp.public_view(session)}


@router.post("/connectors/connect")
async def connect(ctx: CtxDep, body: ConnectRequest) -> dict[str, Any]:
    provider = normalize_connector_provider(body.provider)
    toolkit = _TOOLKIT_FOR_PROVIDER[provider]

    if not settings.composio_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{provider} is not configured. Set COMPOSIO_API_KEY to enable OAuth.",
        )

    composio_user_id = f"{settings.composio_user_prefix}:{ctx.workspace_id}"
    callback = body.redirect_url or f"{settings.web_origin}/settings/integrations"
    # Composio rejects a second connected account with an alias that is already
    # taken for the same entity, so the alias must identify this link attempt,
    # not the provider/workspace pair.
    alias = f"{provider}-{db_module.new_id()[:8]}"
    try:
        auth_config = await composio.resolve_auth_config(toolkit)
        link = await composio.create_connect_link(
            auth_config_id=auth_config["id"],
            user_id=composio_user_id,
            callback_url=callback,
            alias=alias,
        )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Composio error: {exc}") from exc

    existing = (
        await ctx.session.execute(
            sa.select(connected_accounts)
            .where(
                connected_accounts.c.workspace_id == ctx.workspace_id,
                connected_accounts.c.provider == provider,
                connected_accounts.c.status != "revoked",
            )
            .order_by(connected_accounts.c.created_at.desc())
        )
    ).mappings().first()

    now = utcnow()
    values = {
        "composio_connected_account_id": link.get("connected_account_id"),
        "composio_auth_config_id": auth_config["id"],
        "composio_user_id": composio_user_id,
        "status": "pending",
        "last_error": None,
        "updated_at": now,
    }
    # A fresh OAuth link must clear any cached provider failure, otherwise a
    # reconnect would stay "broken" until the short failure TTL expires.
    if existing:
        clear_provider_failure(existing.get("composio_connected_account_id"))
    clear_provider_failure(link.get("connected_account_id"))
    if existing:
        account_id = existing["id"]
        await ctx.session.execute(
            sa.update(connected_accounts).where(connected_accounts.c.id == account_id).values(**values)
        )
    else:
        account_id = db_module.new_id()
        await ctx.session.execute(
            sa.insert(connected_accounts).values(
                id=account_id,
                workspace_id=ctx.workspace_id,
                recruiter_id=ctx.user_id,
                provider=provider,
                connected_email=None,
                display_name=None,
                scopes=[],
                created_at=now,
                **values,
            )
        )
    await ctx.session.commit()
    composio.forget_mcp_session(composio_mcp.composio_user_id(ctx.workspace_id))
    return {
        "provider": provider,
        "status": "pending",
        "connected_account_id": account_id,
        "redirect_url": link.get("redirect_url"),
        "message": "Complete the OAuth flow, then press Verify connection.",
    }


@router.post("/connectors/{account_id}/verify")
async def verify_connector(ctx: CtxDep, account_id: str) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            sa.select(connected_accounts).where(
                connected_accounts.c.id == account_id,
                connected_accounts.c.workspace_id == ctx.workspace_id,
            )
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connector not found")
    if not row["composio_connected_account_id"] or not settings.composio_enabled:
        return {"status": row["status"], "connected_email": row["connected_email"]}

    try:
        remote = await composio.get_connected_account(row["composio_connected_account_id"])
    except IntegrationError as exc:
        await ctx.session.execute(
            sa.update(connected_accounts)
            .where(connected_accounts.c.id == account_id)
            .values(last_error=str(exc)[:400], status="error", updated_at=utcnow())
        )
        await ctx.session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Composio error: {exc}") from exc

    if not remote:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connected account not found in Composio")

    remote_status = str(remote.get("status") or "").upper()
    mapping = {
        "ACTIVE": "active",
        "INITIALIZING": "pending",
        "INITIATED": "pending",
        "PENDING": "pending",
        "EXPIRED": "expired",
        "REVOKED": "revoked",
        "FAILED": "error",
    }
    new_status = mapping.get(remote_status, "pending")
    email = extract_email_from_account(remote)
    await ctx.session.execute(
        sa.update(connected_accounts)
        .where(connected_accounts.c.id == account_id)
        .values(
            status=new_status,
            connected_email=email or row["connected_email"],
            display_name=(email or remote.get("alias") or row["display_name"]),
            last_verified_at=utcnow(),
            last_error=None,
            updated_at=utcnow(),
        )
    )
    await ctx.session.commit()
    if new_status == "active":
        # Account is healthy again: drop the cached failure so the next meeting
        # attempt actually reaches the provider.
        clear_provider_failure(row.get("composio_connected_account_id"))
    if new_status == "active" and row["provider"] == "gmail":
        await ing.ensure_sync_settings(
            ctx.session, workspace_id=ctx.workspace_id, account_id=account_id
        )
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="connector.verified",
        entity_type="connected_account",
        entity_id=account_id,
        metadata={"provider": row["provider"], "status": new_status},
    )
    await ctx.session.commit()
    return {"status": new_status, "connected_email": email or row["connected_email"]}


@router.delete("/connectors/{account_id}")
async def disconnect(ctx: CtxDep, account_id: str) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            sa.select(connected_accounts).where(
                connected_accounts.c.id == account_id,
                connected_accounts.c.workspace_id == ctx.workspace_id,
            )
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connector not found")

    if row["composio_connected_account_id"] and settings.composio_enabled:
        try:
            await composio.delete_connected_account(row["composio_connected_account_id"])
        except IntegrationError as exc:
            if exc.code != "not_found":
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Composio error: {exc}") from exc

    await ctx.session.execute(
        sa.update(connected_accounts)
        .where(connected_accounts.c.id == account_id)
        .values(status="revoked", updated_at=utcnow())
    )
    await ctx.session.execute(
        sa.update(inbox_sync_settings)
        .where(inbox_sync_settings.c.connected_account_id == account_id)
        .values(enabled=False, next_sync_at=None, updated_at=utcnow())
    )
    await ctx.session.commit()
    composio.forget_mcp_session(composio_mcp.composio_user_id(ctx.workspace_id))
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="connector.disconnected",
        entity_type="connected_account",
        entity_id=account_id,
        metadata={"provider": row["provider"]},
    )
    await ctx.session.commit()
    return {"status": "revoked", "id": account_id}


# --------------------------------------------------------------------- sync
@router.get("/sync/settings")
async def sync_settings(ctx: CtxDep) -> dict[str, Any]:
    account = (
        await ctx.session.execute(
            sa.select(connected_accounts)
            .where(
                connected_accounts.c.workspace_id == ctx.workspace_id,
                connected_accounts.c.provider == "gmail",
            )
            .order_by(
                # Prefer the connection sync would actually use; a revoked row
                # must not shadow the current one just because it is older.
                (connected_accounts.c.status == "active").desc(),
                (connected_accounts.c.status != "revoked").desc(),
                connected_accounts.c.created_at.desc(),
            )
            .limit(1)
        )
    ).mappings().first()
    if not account:
        return {"connected": False, "presets": SYNC_PRESETS}
    row = (
        await ctx.session.execute(
            sa.select(inbox_sync_settings).where(
                inbox_sync_settings.c.connected_account_id == account["id"]
            )
        )
    ).mappings().first()
    if not row:
        row = await ing.ensure_sync_settings(
            ctx.session, workspace_id=ctx.workspace_id, account_id=account["id"]
        )
    return {
        "connected": True,
        "connected_email": account["connected_email"],
        "connector_status": account["status"],
        "presets": SYNC_PRESETS,
        "settings": {
            "id": row["id"],
            "enabled": row["enabled"],
            "interval_minutes": row["interval_minutes"],
            "is_custom_interval": row["is_custom_interval"],
            "last_synced_at": row["last_synced_at"],
            "next_sync_at": row["next_sync_at"],
            "last_sync_status": row["last_sync_status"],
            "last_sync_error": row["last_sync_error"],
            "consecutive_failures": row["consecutive_failures"],
            "query": row["query"],
        },
    }


@router.put("/sync/settings")
async def update_sync_settings(ctx: CtxDep, body: SyncSettingsIn) -> dict[str, Any]:
    account = await find_account(ctx.session, ctx.workspace_id, "gmail")
    if not account:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connect Gmail before configuring sync")
    row = await ing.ensure_sync_settings(
        ctx.session, workspace_id=ctx.workspace_id, account_id=account["id"]
    )
    values: dict[str, Any] = {"updated_at": utcnow()}
    if body.enabled is not None:
        values["enabled"] = body.enabled
        values["next_sync_at"] = utcnow() if body.enabled else None
    if body.interval_minutes is not None:
        values["interval_minutes"] = body.interval_minutes
        values["is_custom_interval"] = body.interval_minutes not in SYNC_PRESETS
        if row.get("last_synced_at"):
            values["next_sync_at"] = row["last_synced_at"] + timedelta(minutes=body.interval_minutes)
    if body.query is not None:
        values["query"] = body.query
    await ctx.session.execute(
        sa.update(inbox_sync_settings).where(inbox_sync_settings.c.id == row["id"]).values(**values)
    )
    await ctx.session.commit()
    return {"status": "updated", "next_sync_at": values.get("next_sync_at", row.get("next_sync_at"))}


@router.post("/sync/now")
async def sync_now(ctx: CtxDep) -> dict[str, Any]:
    account = await find_account(ctx.session, ctx.workspace_id, "gmail")
    if not account:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connect Gmail first")
    active = await ing.active_job_for_account(ctx.session, account["id"])
    if active and active["status"] == "running":
        return {"status": "already_running", "job_id": active["id"]}
    job = await ing.enqueue_job(
        ctx.session, workspace_id=ctx.workspace_id, account_id=account["id"], trigger="manual"
    )
    result = await ing.run_job(ctx.session, job=job, worker_id=f"manual:{ctx.user_id}")
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="inbox.sync_requested",
        entity_type="ingestion_job",
        entity_id=job["id"],
        metadata={"trigger": "manual"},
    )
    await ctx.session.commit()
    return result


@router.get("/sync/jobs")
async def sync_jobs(ctx: CtxDep, limit: int = Query(default=20, le=100)) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(ingestion_jobs)
            .where(ingestion_jobs.c.workspace_id == ctx.workspace_id)
            .order_by(ingestion_jobs.c.created_at.desc())
            .limit(limit)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------- candidates
@router.get("/candidates")
async def get_candidates(
    ctx: CtxDep,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    status_filter: str | None = Query(default=None, alias="status"),
    location: str | None = None,
    min_experience_years: float | None = None,
    skills: str | None = Query(default=None, description="comma separated"),
) -> list[dict[str, Any]]:
    filters: dict[str, Any] = {
        "location": location,
        "min_experience_years": min_experience_years,
        "pipeline_status": [status_filter] if status_filter else [],
        "skills": [s.strip() for s in (skills or "").split(",") if s.strip()],
    }
    return await list_candidates(ctx.session, ctx.workspace_id, filters=filters, limit=limit, offset=offset)


@router.post("/candidates/search")
async def search_candidates(ctx: CtxDep, body: CandidateFilters) -> list[dict[str, Any]]:
    filters = body.model_dump()
    return await list_candidates(
        ctx.session, ctx.workspace_id, filters=filters, limit=body.limit
    )


@router.get("/candidates/{candidate_id}")
async def get_candidate_detail(ctx: CtxDep, candidate_id: str) -> dict[str, Any]:
    detail = await candidate_detail(ctx.session, ctx.workspace_id, candidate_id)
    if not detail:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return detail


@router.patch("/candidates/{candidate_id}/status")
async def update_status(ctx: CtxDep, candidate_id: str, body: StatusUpdate) -> dict[str, Any]:
    previous = await set_candidate_status(
        ctx.session,
        workspace_id=ctx.workspace_id,
        candidate_id=candidate_id,
        to_status=body.status,
        changed_by=ctx.user_id,
        reason=body.reason,
    )
    if previous is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="candidate.status_changed",
        entity_type="candidate",
        entity_id=candidate_id,
        metadata={"from": previous, "to": body.status, "reason": body.reason},
    )
    await ctx.session.commit()
    return {"status": body.status, "previous": previous}


@router.post("/candidates/{candidate_id}/notes")
async def add_note(ctx: CtxDep, candidate_id: str, body: NoteCreate) -> dict[str, Any]:
    candidate = await get_candidate(ctx.session, ctx.workspace_id, candidate_id)
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    note_id = db_module.new_id()
    await ctx.session.execute(
        sa.insert(recruiter_notes).values(
            id=note_id,
            workspace_id=ctx.workspace_id,
            candidate_id=candidate_id,
            recruiter_id=ctx.user_id,
            note=body.note,
            next_followup_at=body.next_followup_at,
            created_at=utcnow(),
        )
    )
    await ctx.session.commit()
    return {"id": note_id, "note": body.note}


@router.get("/candidates/{candidate_id}/resume")
async def download_resume(ctx: CtxDep, candidate_id: str) -> Response:
    candidate = await get_candidate(ctx.session, ctx.workspace_id, candidate_id)
    if not candidate or not candidate.get("resume_storage_path"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume not found")
    try:
        blob = await storage.get(candidate["resume_storage_path"])
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume file missing from storage") from exc
    filename = candidate.get("resume_filename") or "resume.pdf"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(
        content=blob,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/candidates/{candidate_id}/attachments")
async def candidate_attachments(ctx: CtxDep, candidate_id: str) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(email_attachments)
            .join(
                db_module.candidate_applications,
                db_module.candidate_applications.c.email_message_id == email_attachments.c.email_message_id,
            )
            .where(
                db_module.candidate_applications.c.candidate_id == candidate_id,
                email_attachments.c.workspace_id == ctx.workspace_id,
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------- jobs
@router.get("/jobs")
async def list_jobs(ctx: CtxDep) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(jobs).where(jobs.c.workspace_id == ctx.workspace_id).order_by(jobs.c.created_at.desc())
        )
    ).mappings().all()
    counts = dict(
        (
            await ctx.session.execute(
                sa.select(candidate_evaluations.c.job_id, sa.func.count())
                .where(candidate_evaluations.c.workspace_id == ctx.workspace_id)
                .group_by(candidate_evaluations.c.job_id)
            )
        ).all()
    )
    return [
        {**dict(row), "candidate_count": int(counts.get(row["id"], 0))}
        for row in rows
    ]


@router.post("/jobs")
async def create_job(ctx: CtxDep, body: JobCreate) -> dict[str, Any]:
    job_id = db_module.new_id()
    now = utcnow()
    await ctx.session.execute(
        sa.insert(jobs).values(
            id=job_id,
            workspace_id=ctx.workspace_id,
            title=body.title,
            description=body.description,
            structured_requirements={},
            status="open",
            application_subject_prefix=body.application_subject_prefix,
            created_by=ctx.user_id,
            created_at=now,
            updated_at=now,
        )
    )
    await ctx.session.commit()
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="job.created",
        entity_type="job",
        entity_id=job_id,
        metadata={"title": body.title, "application_subject_prefix": body.application_subject_prefix},
    )
    await ctx.session.commit()
    return {
        "id": job_id,
        "title": body.title,
        "status": "open",
        "application_subject_prefix": body.application_subject_prefix,
    }


@router.patch("/jobs/{job_id}")
async def update_job(ctx: CtxDep, job_id: str, body: JobUpdate) -> dict[str, Any]:
    existing = (
        await ctx.session.execute(
            sa.select(jobs).where(jobs.c.id == job_id, jobs.c.workspace_id == ctx.workspace_id)
        )
    ).mappings().first()
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return dict(existing)

    updates["updated_at"] = utcnow()
    await ctx.session.execute(sa.update(jobs).where(jobs.c.id == job_id).values(**updates))
    await ctx.session.commit()

    row = (
        await ctx.session.execute(
            sa.select(jobs).where(jobs.c.id == job_id, jobs.c.workspace_id == ctx.workspace_id)
        )
    ).mappings().first()
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="job.updated",
        entity_type="job",
        entity_id=job_id,
        metadata={"fields": sorted(updates)},
    )
    await ctx.session.commit()
    return dict(row)


@router.get("/jobs/{job_id}")
async def get_job(ctx: CtxDep, job_id: str) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            sa.select(jobs).where(jobs.c.id == job_id, jobs.c.workspace_id == ctx.workspace_id)
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return dict(row)


@router.post("/jobs/{job_id}/evaluate")
async def evaluate_job(
    ctx: CtxDep,
    job_id: str,
    top: int = Query(default=10, ge=1, le=50),
    verify: bool = Query(default=True),
) -> dict[str, Any]:
    job = (
        await ctx.session.execute(
            sa.select(jobs).where(jobs.c.id == job_id, jobs.c.workspace_id == ctx.workspace_id)
        )
    ).mappings().first()
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")

    result = await run_evaluation(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        job_id=job_id,
        description=job["description"],
        title=job["title"],
        limit=25,
        verify=verify,
    )
    requirements = result.get("requirements")
    if requirements is not None:
        await ctx.session.execute(
            sa.update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                structured_requirements=requirements.model_dump(mode="json"),
                updated_at=utcnow(),
            )
        )
        await ctx.session.commit()
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="evaluation.completed",
        entity_type="job",
        entity_id=job_id,
        metadata={"evaluated": len(result["ranked"]), "method": result.get("requirement_method")},
    )
    await ctx.session.commit()
    ranked = result["ranked"]
    return {
        "job_id": job_id,
        "requirements": requirements.model_dump(mode="json") if requirements else None,
        "requirement_method": result.get("requirement_method"),
        "candidate_pool": result.get("candidate_pool"),
        "evaluated": len(ranked),
        "results": ranked[:top],
        "errors": result.get("errors"),
    }


@router.get("/jobs/{job_id}/evaluations")
async def job_evaluations(ctx: CtxDep, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(candidate_evaluations, candidates)
            .join(candidates, candidates.c.id == candidate_evaluations.c.candidate_id)
            .where(
                candidate_evaluations.c.job_id == job_id,
                candidate_evaluations.c.workspace_id == ctx.workspace_id,
            )
            .order_by(candidate_evaluations.c.match_score.desc())
            .limit(limit)
        )
    ).all()
    out = []
    for evaluation, candidate in rows:
        item = dict(evaluation)
        item["candidate"] = candidate_summary(dict(candidate))
        out.append(item)
    return out


# --------------------------------------------------------------- interviews
@router.get("/interviews")
async def list_interviews(ctx: CtxDep, limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    return await upcoming_interviews(ctx.session, ctx.workspace_id, limit=limit)


class AvailabilityRequest(BaseModel):
    start: datetime | None = None
    duration_minutes: int = Field(default=30, ge=15, le=240)
    timezone: str | None = None


@router.post("/interviews/availability")
async def check_availability(ctx: CtxDep, body: AvailabilityRequest) -> dict[str, Any]:
    return await availability(
        ctx.session,
        workspace_id=ctx.workspace_id,
        start=body.start,
        duration_minutes=body.duration_minutes,
        timezone_name=body.timezone or settings.default_timezone,
    )


@router.post("/interviews/schedule")
async def schedule_interview(ctx: CtxDep, body: ScheduleRequest) -> dict[str, Any]:
    candidate = await get_candidate(ctx.session, ctx.workspace_id, body.candidate_id)
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    try:
        result = await run_scheduling(
            session=ctx.session,
            workspace_id=ctx.workspace_id,
            recruiter_id=ctx.user_id,
            candidate_id=body.candidate_id,
            request=body,
        )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await log_activity(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        action="interview.schedule_requested",
        entity_type="candidate",
        entity_id=body.candidate_id,
        metadata={"status": result.get("status")},
    )
    await ctx.session.commit()
    return result


@router.post("/interviews/{interview_id}/cancel")
async def cancel(ctx: CtxDep, interview_id: str) -> dict[str, Any]:
    try:
        return await cancel_interview(
            ctx.session, workspace_id=ctx.workspace_id, interview_id=interview_id, recruiter_id=ctx.user_id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/interviews/{interview_id}/reschedule")
async def reschedule(ctx: CtxDep, interview_id: str, body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("start")
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start is required")
    start = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    try:
        return await reschedule_interview(
            ctx.session,
            workspace_id=ctx.workspace_id,
            interview_id=interview_id,
            new_start=start,
            recruiter_id=ctx.user_id,
        )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# ------------------------------------------------------------ communications
@router.get("/communications")
async def list_communications(ctx: CtxDep, limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
    return await comms.recent_communications(ctx.session, ctx.workspace_id, limit=limit)


@router.post("/communications/draft")
async def draft_communication(ctx: CtxDep, body: SendEmailRequest) -> dict[str, Any]:
    candidate = await get_candidate(ctx.session, ctx.workspace_id, body.candidate_id)
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    if not candidate.get("email"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Candidate has no email address")

    if body.subject and body.body:
        subject, content, method = body.subject, body.body, "manual"
    else:
        profile = await get_workspace_profile(ctx.session, ctx.workspace_id)
        subject, content, method = await comms.draft_email(
            kind=body.kind,
            candidate=candidate,
            instructions=body.body,
            company_name=profile["company_name"] or None,
            interviewer_name=profile["interviewer_name"] or None,
            company_phone=profile["company_phone"] or None,
        )
    comm = await comms.queue_email(
        ctx.session,
        workspace_id=ctx.workspace_id,
        candidate=candidate,
        recruiter_id=ctx.user_id,
        kind=body.kind,
        subject=subject,
        body=content,
        method=method,
    )
    action = await create_pending_action(
        ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        kind="send_email",
        title=f"Send {body.kind.replace('_', ' ')} to {candidate['full_name']}",
        payload={"communication_id": comm["id"], "candidate_id": candidate["id"], "subject": subject},
    )
    if body.approve:
        executed = await execute_pending_action(
            ctx.session, workspace_id=ctx.workspace_id, action_id=action["id"], recruiter_id=ctx.user_id
        )
        return {"communication": comm, "action": action, "execution": executed}
    return {"communication": comm, "action": action}


@router.post("/communications/{communication_id}/send")
async def send_communication(ctx: CtxDep, communication_id: str) -> dict[str, Any]:
    try:
        return await comms.send_communication(
            ctx.session,
            workspace_id=ctx.workspace_id,
            communication_id=communication_id,
            recruiter_id=ctx.user_id,
        )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/actions")
async def actions(ctx: CtxDep, status_filter: str = Query(default="pending", alias="status")) -> list[dict[str, Any]]:
    return await list_pending_actions(ctx.session, ctx.workspace_id, status=status_filter)


@router.post("/actions/{action_id}/approve")
async def approve_action(ctx: CtxDep, action_id: str) -> dict[str, Any]:
    try:
        return await execute_pending_action(
            ctx.session, workspace_id=ctx.workspace_id, action_id=action_id, recruiter_id=ctx.user_id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/actions/{action_id}/reject")
async def reject_action(ctx: CtxDep, action_id: str) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            sa.select(pending_actions).where(
                pending_actions.c.id == action_id,
                pending_actions.c.workspace_id == ctx.workspace_id,
            )
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    await ctx.session.execute(
        sa.update(pending_actions)
        .where(pending_actions.c.id == action_id)
        .values(status="rejected", executed_at=utcnow())
    )
    payload = row["payload"] or {}
    if row["kind"] == "send_email" and payload.get("communication_id"):
        await ctx.session.execute(
            sa.update(candidate_communications)
            .where(candidate_communications.c.id == payload["communication_id"])
            .values(status="rejected")
        )
    await ctx.session.commit()
    return {"status": "rejected", "id": action_id}


# --------------------------------------------------------------------- chat
async def resolve_thread(ctx: CtxDep, body: ChatRequest) -> str:
    """Runs before the endpoint body, so a bad thread id is a normal 404."""
    if body.thread_id:
        exists = (
            await ctx.session.execute(
                sa.select(chat_threads.c.id).where(
                    chat_threads.c.id == body.thread_id, chat_threads.c.workspace_id == ctx.workspace_id
                )
            )
        ).scalar_one_or_none()
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
        return body.thread_id
    thread_id = db_module.new_id()
    now = utcnow()
    await ctx.session.execute(
        sa.insert(chat_threads).values(
            id=thread_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            title=body.message[:80],
            created_at=now,
            updated_at=now,
        )
    )
    await ctx.session.commit()
    return thread_id


ThreadDep = Annotated[str, Depends(resolve_thread)]


@router.post("/chat")
async def chat(ctx: CtxDep, body: ChatRequest, thread_id: ThreadDep) -> dict[str, Any]:
    result = await run_assistant(
        session=ctx.session,
        workspace_id=ctx.workspace_id,
        recruiter_id=ctx.user_id,
        thread_id=thread_id,
        user_message=body.message,
        job_id=body.job_id,
    )
    await ctx.session.execute(
        sa.update(chat_threads).where(chat_threads.c.id == thread_id).values(updated_at=utcnow())
    )
    await ctx.session.commit()
    return result


@router.post("/chat/stream", response_class=EventSourceResponse)
async def chat_stream(
    ctx: CtxDep, body: ChatRequest, thread_id: ThreadDep
) -> AsyncIterator[ServerSentEvent]:
    """Same turn as ``/chat`` but streamed as Server-Sent Events.

    Each tool step is emitted when it starts and when it finishes (with its
    input, result summary and duration), then the reply text arrives in
    chunks, and a ``done`` event carries the structured payload (candidates,
    pending approvals).
    """
    try:
        async for event in run_assistant_stream(
            session=ctx.session,
            workspace_id=ctx.workspace_id,
            recruiter_id=ctx.user_id,
            thread_id=thread_id,
            user_message=body.message,
            job_id=body.job_id,
        ):
            yield ServerSentEvent(data=event, event=event["type"])
    except Exception as exc:  # noqa: BLE001
        log.exception("chat stream failed")
        yield ServerSentEvent(data={"type": "error", "message": str(exc)[:300]}, event="error")


@router.get("/chat/threads")
async def list_threads(ctx: CtxDep, limit: int = Query(default=50, le=100)) -> list[dict[str, Any]]:
    rows = (
        await ctx.session.execute(
            sa.select(chat_threads)
            .where(chat_threads.c.workspace_id == ctx.workspace_id)
            .order_by(chat_threads.c.updated_at.desc())
            .limit(limit)
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.delete("/chat/{thread_id}")
async def delete_thread(ctx: CtxDep, thread_id: str) -> dict[str, Any]:
    await ctx.session.execute(
        sa.delete(chat_threads).where(
            chat_threads.c.id == thread_id, chat_threads.c.workspace_id == ctx.workspace_id
        )
    )
    await ctx.session.commit()
    return {"status": "deleted", "thread_id": thread_id}


@router.get("/chat/{thread_id}")
async def get_thread(ctx: CtxDep, thread_id: str) -> dict[str, Any]:
    thread = (
        await ctx.session.execute(
            sa.select(chat_threads).where(
                chat_threads.c.id == thread_id, chat_threads.c.workspace_id == ctx.workspace_id
            )
        )
    ).mappings().first()
    if not thread:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    messages = (
        await ctx.session.execute(
            sa.select(db_module.chat_messages)
            .where(db_module.chat_messages.c.thread_id == thread_id)
            .order_by(db_module.chat_messages.c.created_at.asc())
            .limit(300)
        )
    ).mappings().all()
    return {"thread": dict(thread), "messages": [dict(m) for m in messages]}
