"""Internal worker endpoints, called by Supabase Cron / Edge Functions.

Authenticated with ``API_INTERNAL_TOKEN`` (constant-time compare), never with a
user session. These routes deliberately expose no candidate data: they return
job statistics only.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status

from ..db import connected_accounts, ingestion_jobs, session, utcnow
from ..security import require_internal
from ..services import ingestion as ing
from ..services.scheduler import tick

log = logging.getLogger("recruiter.internal")

router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal)])


@router.post("/cron/tick")
async def cron_tick(max_jobs: int = 5) -> dict[str, Any]:
    """One scheduler pass: enqueue due inboxes and drain due jobs."""
    async with session() as db:
        return await tick(db, worker_id="supabase-cron", max_jobs=min(max(max_jobs, 1), 20))


@router.post("/sync/{account_id}")
async def sync_account(account_id: str, trigger: str = "cron") -> dict[str, Any]:
    async with session() as db:
        account = (
            await db.execute(
                sa.select(connected_accounts).where(connected_accounts.c.id == account_id)
            )
        ).mappings().first()
        if not account:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Connected account not found")
        if account["status"] != "active":
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Connected account is {account['status']}, reconnect required"
            )
        job = await ing.enqueue_job(
            db, workspace_id=account["workspace_id"], account_id=account_id, trigger=trigger
        )
        if job["status"] == "running":
            return {"status": "already_running", "job_id": job["id"]}
        return await ing.run_job(db, job=job, worker_id="edge-worker")


@router.post("/recover")
async def recover_stuck_jobs(stale_minutes: int = 15) -> dict[str, Any]:
    """Return jobs orphaned by a crashed worker to the queue."""
    cutoff = utcnow() - timedelta(minutes=stale_minutes)
    async with session() as db:
        result = await db.execute(
            sa.update(ingestion_jobs)
            .where(
                ingestion_jobs.c.status == "running",
                ingestion_jobs.c.locked_at.isnot(None),
                ingestion_jobs.c.locked_at < cutoff,
            )
            .values(status="retrying", next_retry_at=utcnow(), locked_by=None, locked_at=None,
                    error_message="Requeued after worker timeout")
        )
        await db.commit()
        return {"requeued": int(result.rowcount or 0)}


@router.get("/due")
async def due_work() -> dict[str, Any]:
    """Read-only view used by the Supabase scheduler Edge Function."""
    async with session() as db:
        accounts = await ing.due_accounts(db)
        jobs = await ing.due_jobs(db)
        return {
            "accounts": [
                {
                    "connected_account_id": row["connected_account_id"],
                    "workspace_id": row["workspace_id"],
                    "next_sync_at": row["next_sync_at"],
                }
                for row in accounts
            ],
            "jobs": [
                {"id": job["id"], "status": job["status"], "attempts": job["attempts"]}
                for job in jobs
            ],
        }
