"""LangGraph workflow: automated Gmail ingestion.

    prepare ─▶ fetch ─▶ ingest ─▶ finalize ─▶ END
                  │        │
                  └────────┴─▶ (error) ─▶ retry ─▶ END

* ``prepare``  – load connection + sync settings (deterministic DB work)
* ``fetch``    – list candidate emails through Composio Gmail
* ``ingest``   – per-email stage machine (dedupe → download → extract → parse → persist)
* ``finalize`` – mark job/sync success and log activity
* ``retry``    – exponential backoff scheduling, failure recording

No node sends an external side effect other than read-only Gmail calls; the
workflow is safe to re-run because all persistence is idempotent.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from ..composio import IntegrationError
from ..db import connected_accounts
from ..repo import log_activity
from ..services import ingestion as ing
from ..services.mail import MailProvider, get_mail_provider

log = logging.getLogger("recruiter.graph.ingestion")


class IngestionState(TypedDict, total=False):
    job: dict[str, Any]
    workspace_id: str
    account_id: str
    worker_id: str
    provider: MailProvider
    settings_row: dict[str, Any]
    messages: list[Any]
    job_by_message_id: dict[str, str]
    stats: dict[str, Any]
    error: str | None
    status: str
    started: float
    claimed: bool


def _new_stats() -> dict[str, Any]:
    return {
        "emails_seen": 0,
        "emails_processed": 0,
        "emails_skipped": 0,
        "emails_duplicate": 0,
        "attachments_skipped": 0,
        "attachments_failed": 0,
        "attachments_duplicate": 0,
        "resumes_parsed": 0,
        "candidates_created": 0,
        "candidates_updated": 0,
        "applications_linked": 0,
    }


def build_ingestion_graph(session: AsyncSession):
    async def prepare(state: IngestionState) -> dict[str, Any]:
        job = state["job"]
        account = (
            await session.execute(
                sa_select_account(job["connected_account_id"])
            )
        ).mappings().first()
        provider = state.get("provider") or get_mail_provider(dict(account) if account else None)
        settings_row = await ing.ensure_sync_settings(
            session, workspace_id=job["workspace_id"], account_id=job["connected_account_id"]
        )
        claimed = await ing.claim_job(session, job["id"], state["worker_id"])
        return {
            "provider": provider,
            "settings_row": settings_row,
            "claimed": claimed,
            "started": time.perf_counter(),
        }

    async def fetch(state: IngestionState) -> dict[str, Any]:
        settings_row = state["settings_row"]
        provider = state["provider"]
        since = None
        if settings_row.get("last_synced_at"):
            since = settings_row["last_synced_at"] - timedelta(
                minutes=settings_row.get("overlap_minutes") or 0
            )
        base_query = (settings_row.get("query") or "").strip()
        rules = await ing.application_subject_rules(session, workspace_id=state["workspace_id"])

        if not rules:
            messages = await provider.list_messages(
                since=since, query=base_query or "newer_than:2d", max_results=50
            )
            return {"messages": messages, "job_by_message_id": {}}

        # One targeted Gmail query per job headline, then verify the real prefix.
        window = base_query or ("newer_than:2d" if since is None else "")
        messages_by_id: dict[str, Any] = {}
        job_by_message_id: dict[str, str] = {}
        for rule in sorted(rules, key=lambda item: len(item["prefix"])):
            query = ing.build_subject_query(window, rule["prefix"])
            batch = await provider.list_messages(since=since, query=query, max_results=50)
            for message in batch:
                provider_message_id = getattr(message, "provider_message_id", None)
                if not provider_message_id:
                    continue
                if not ing.subject_matches_rule(getattr(message, "subject", None), rule["prefix"]):
                    continue
                messages_by_id.setdefault(provider_message_id, message)
                # Rules are iterated shortest → longest, so the most specific wins.
                job_by_message_id[provider_message_id] = rule["job_id"]
        return {"messages": list(messages_by_id.values()), "job_by_message_id": job_by_message_id}

    async def ingest(state: IngestionState) -> dict[str, Any]:
        stats = _new_stats()
        stats["emails_seen"] = len(state.get("messages") or [])
        job = state["job"]
        job_by_message_id = state.get("job_by_message_id") or {}
        for message in state.get("messages") or []:
            await ing.process_message(
                session,
                workspace_id=job["workspace_id"],
                account_id=job["connected_account_id"],
                message=message,
                provider=state["provider"],
                stats=stats,
                job_id=job_by_message_id.get(message.provider_message_id),
            )
        return {"stats": stats}

    async def finalize(state: IngestionState) -> dict[str, Any]:
        job = state["job"]
        stats = state.get("stats") or _new_stats()
        stats["duration_ms"] = int((time.perf_counter() - state.get("started", time.perf_counter())) * 1000)
        status = "partial" if stats.get("attachments_failed") else "succeeded"
        await ing.finish_job(session, job["id"], status=status, stats=stats)
        await ing.mark_sync_success(session, state["settings_row"], stats=stats)
        await log_activity(
            session,
            workspace_id=job["workspace_id"],
            recruiter_id=None,
            action="inbox.sync_completed",
            entity_type="ingestion_job",
            entity_id=job["id"],
            metadata={k: v for k, v in stats.items() if not isinstance(v, list)},
        )
        await session.commit()
        return {"status": status}

    async def handle_error(state: IngestionState) -> dict[str, Any]:
        job = state["job"]
        error = state.get("error") or "unknown ingestion error"
        stats = state.get("stats") or _new_stats()
        stats["duration_ms"] = int((time.perf_counter() - state.get("started", time.perf_counter())) * 1000)
        attempts = int(job.get("attempts") or 0) + 1
        will_retry = await ing.schedule_retry(session, job["id"], attempts=attempts, error=error, stats=stats)
        await ing.mark_sync_failure(session, state["settings_row"], error=error, stats=stats)
        await log_activity(
            session,
            workspace_id=job["workspace_id"],
            recruiter_id=None,
            action="inbox.sync_failed",
            entity_type="ingestion_job",
            entity_id=job["id"],
            metadata={"error": error[:300], "will_retry": will_retry},
        )
        await session.commit()
        return {"status": "retrying" if will_retry else "failed"}

    def wrap(fn):
        async def node(state: IngestionState) -> dict[str, Any]:
            try:
                return await fn(state)
            except IntegrationError as exc:
                log.warning("ingestion integration error: %s", exc)
                return {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("ingestion node crashed")
                return {"error": f"{exc.__class__.__name__}: {exc}"}

        return node

    graph = StateGraph(IngestionState)
    graph.add_node("prepare", wrap(prepare))
    graph.add_node("fetch", wrap(fetch))
    graph.add_node("ingest", wrap(ingest))
    graph.add_node("finalize", wrap(finalize))
    graph.add_node("handle_error", wrap(handle_error))

    graph.set_entry_point("prepare")

    def after_prepare(state: IngestionState) -> str:
        if state.get("error"):
            return "handle_error"
        if not state.get("claimed"):
            return "skipped"
        return "fetch"

    def after_step(state: IngestionState) -> str:
        return "handle_error" if state.get("error") else "continue"

    graph.add_conditional_edges(
        "prepare", after_prepare, {"fetch": "fetch", "handle_error": "handle_error", "skipped": END}
    )
    graph.add_conditional_edges("fetch", after_step, {"continue": "ingest", "handle_error": "handle_error"})
    graph.add_conditional_edges("ingest", after_step, {"continue": "finalize", "handle_error": "handle_error"})
    graph.add_edge("finalize", END)
    graph.add_edge("handle_error", END)
    return graph.compile()


def sa_select_account(account_id: str):
    import sqlalchemy as sa

    return sa.select(connected_accounts).where(connected_accounts.c.id == account_id)


async def run_ingestion(
    *,
    session: AsyncSession,
    job: dict[str, Any],
    worker_id: str = "worker",
    provider: MailProvider | None = None,
) -> dict[str, Any]:
    compiled = build_ingestion_graph(session)
    state: IngestionState = {
        "job": job,
        "workspace_id": job["workspace_id"],
        "account_id": job["connected_account_id"],
        "worker_id": worker_id,
        "error": None,
    }
    if provider is not None:
        state["provider"] = provider
    result = await compiled.ainvoke(state)
    return {
        "job_id": job["id"],
        "status": result.get("status") or "not_claimed",
        "stats": result.get("stats") or {},
        "error": result.get("error"),
    }
