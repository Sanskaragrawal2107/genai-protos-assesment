"""Ingestion: duplicate protection, stage tracking, retry/backoff."""
from __future__ import annotations

import sqlalchemy as sa
import pytest

from app import db as db_module
from app.composio import IntegrationError
from app.db import (
    candidates,
    email_attachments,
    email_messages,
    ingestion_jobs,
    inbox_sync_settings,
)
from app.graphs.ingestion_graph import run_ingestion
from app.services import ingestion as ing
from app.services.mail import MailMessage
from support import SampleMailbox


async def _job(db, workspace):
    return await ing.enqueue_job(
        db, workspace_id=workspace["workspace_id"], account_id=workspace["account_id"], trigger="manual"
    )


async def test_first_sync_imports_candidates(db, workspace):
    job = await _job(db, workspace)
    result = await run_ingestion(session=db, job=job, worker_id="test", provider=SampleMailbox())

    assert result["status"] == "succeeded"
    stats = result["stats"]
    assert stats["emails_seen"] == 4
    assert stats["candidates_created"] == 3
    assert stats["resumes_parsed"] == 3
    # the newsletter email has only an image attachment
    assert stats["emails_skipped"] == 1
    # the image attachment is never downloaded or stored
    stored = (
        await db.execute(sa.select(email_attachments.c.filename))
    ).scalars().all()
    assert all("banner" not in (name or "") for name in stored)
    assert len(stored) == 3

    names = {
        row[0]
        for row in (
            await db.execute(sa.select(candidates.c.full_name).where(candidates.c.workspace_id == workspace["workspace_id"]))
        ).all()
    }
    assert names == {"Priya Nair", "Arjun Mehta", "Sana Kapoor"}

    # parsed attachments tracked separately from the email
    parsed = (
        await db.execute(sa.select(sa.func.count()).select_from(email_attachments).where(email_attachments.c.status == "parsed"))
    ).scalar_one()
    assert parsed == 3


async def test_second_sync_is_idempotent(db, workspace):
    first = await _job(db, workspace)
    await run_ingestion(session=db, job=first, worker_id="test", provider=SampleMailbox())

    # Force a full re-scan window so every message is offered to the importer again
    await db.execute(
        sa.update(inbox_sync_settings)
        .where(inbox_sync_settings.c.connected_account_id == workspace["account_id"])
        .values(last_synced_at=None, next_sync_at=db_module.utcnow())
    )
    await db.commit()

    second = await _job(db, workspace)
    result = await run_ingestion(session=db, job=second, worker_id="test", provider=SampleMailbox())

    assert result["stats"]["emails_duplicate"] == 4
    assert result["stats"]["candidates_created"] == 0
    total = (
        await db.execute(sa.select(sa.func.count()).select_from(candidates))
    ).scalar_one()
    assert total == 3
    emails = (
        await db.execute(sa.select(sa.func.count()).select_from(email_messages))
    ).scalar_one()
    assert emails == 4


async def test_sync_settings_advance_and_records_success(db, workspace):
    job = await _job(db, workspace)
    await run_ingestion(session=db, job=job, worker_id="test", provider=SampleMailbox())

    row = (
        await db.execute(
            sa.select(inbox_sync_settings).where(
                inbox_sync_settings.c.connected_account_id == workspace["account_id"]
            )
        )
    ).mappings().first()
    assert row["last_sync_status"] == "success"
    assert row["last_synced_at"] is not None
    assert row["next_sync_at"] > row["last_synced_at"]
    assert row["consecutive_failures"] == 0


class FailingGmail(SampleMailbox):
    async def list_messages(self, *, since, query, max_results=25):
        raise IntegrationError("Gmail authorization expired", provider="gmail", code="auth_required")


async def test_failure_schedules_retry_with_backoff(db, workspace):
    job = await _job(db, workspace)
    result = await run_ingestion(session=db, job=job, worker_id="test", provider=FailingGmail())

    assert result["status"] == "retrying"
    row = (
        await db.execute(sa.select(ingestion_jobs).where(ingestion_jobs.c.id == job["id"]))
    ).mappings().first()
    assert row["attempts"] == 1
    assert row["next_retry_at"] is not None
    assert "expired" in row["error_message"]

    settings_row = (
        await db.execute(
            sa.select(inbox_sync_settings).where(
                inbox_sync_settings.c.connected_account_id == workspace["account_id"]
            )
        )
    ).mappings().first()
    assert settings_row["last_sync_status"] == "failed"
    assert settings_row["consecutive_failures"] == 1
    assert "expired" in settings_row["last_sync_error"]


async def test_retries_exhaust_after_max_attempts(db, workspace):
    job = await _job(db, workspace)
    for _ in range(4):
        await run_ingestion(session=db, job=job, worker_id="test", provider=FailingGmail())
        job = (
            await db.execute(sa.select(ingestion_jobs).where(ingestion_jobs.c.id == job["id"]))
        ).mappings().first()
        job = dict(job)

    row = (
        await db.execute(sa.select(ingestion_jobs).where(ingestion_jobs.c.id == job["id"]))
    ).mappings().first()
    assert row["status"] == "failed"
    assert row["attempts"] == 4


def test_backoff_grows_and_is_capped():
    delays = [ing.backoff_seconds(n) for n in (1, 2, 3, 4, 8, 20)]
    assert delays == sorted(delays)
    assert delays[0] == 60
    assert delays[-1] <= 6 * 3600


async def test_concurrent_claim_only_succeeds_once(db, workspace):
    job = await _job(db, workspace)
    first = await ing.claim_job(db, job["id"], "worker-a")
    second = await ing.claim_job(db, job["id"], "worker-b")
    assert first is True
    assert second is False
    assert await ing.active_job_for_account(db, workspace["account_id"]) is not None


async def test_unrelated_email_is_skipped_not_failed(db, workspace):
    job = await _job(db, workspace)
    provider = SampleMailbox()
    result = await run_ingestion(session=db, job=job, worker_id="test", provider=provider)
    assert result["status"] == "succeeded"
    skipped = (
        await db.execute(
            sa.select(email_messages.c.subject).where(email_messages.c.processing_status == "skipped")
        )
    ).scalars().all()
    assert any("This week in AI" in (s or "") for s in skipped)
