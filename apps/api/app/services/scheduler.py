"""Scheduler orchestration.

In production Supabase Cron calls the ``scheduler`` Edge Function every minute;
that function invokes this tick over HTTP (``POST /internal/cron/tick``). Locally
an in-process loop calls the same tick every 60 seconds. One function, one
behaviour, both environments — no long-running Edge Function.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import ingestion as ing

log = logging.getLogger("recruiter.scheduler")


async def tick(
    session: AsyncSession,
    *,
    worker_id: str = "scheduler",
    run_jobs: bool = True,
    max_jobs: int = 5,
) -> dict[str, Any]:
    enqueued: list[str] = []
    accounts = await ing.due_accounts(session)
    active = await ing.active_job_account_ids(session, [row["connected_account_id"] for row in accounts])
    for row in accounts:
        if row["connected_account_id"] in active:
            continue
        job = await ing.enqueue_job(
            session,
            workspace_id=row["workspace_id"],
            account_id=row["connected_account_id"],
            trigger="cron",
            check_active=False,
        )
        enqueued.append(job["id"])

    processed: list[dict[str, Any]] = []
    if run_jobs:
        for job in await ing.due_jobs(session, limit=max_jobs):
            if job["status"] == "running":
                continue
            try:
                processed.append(await ing.run_job(session, job=job, worker_id=worker_id))
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the tick
                log.exception("ingestion job %s crashed", job.get("id"))
                processed.append(
                    {"job_id": job.get("id"), "status": "failed", "error": str(exc)[:300]}
                )

    return {"enqueued": enqueued, "processed": processed}
