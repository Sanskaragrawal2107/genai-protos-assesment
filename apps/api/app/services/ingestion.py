"""Automated Gmail resume ingestion.

Stage model (each stage is persisted, so a failure never looks like a success):

    email_messages.processing_status  pending → processing → processed | skipped | failed
    email_attachments.status          pending → downloaded → parsed | failed | skipped
    ingestion_jobs.status             queued → running → succeeded | partial | retrying | failed

Idempotency is enforced by database constraints, not by application memory:

* ``uq_email_provider_id``  – the same Gmail message is never imported twice;
* ``uq_attachment_provider_id`` – the same attachment is never downloaded twice;
* ``uq_candidate_email`` – a candidate is upserted per workspace email;
* content SHA-256 – identical resume bytes are stored once.

Concurrency: a job is claimed with a conditional UPDATE
(``WHERE id=:id AND status IN ('queued','retrying')``) and the scheduler refuses
to create a second active job for an account. No advisory locks are required.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..composio import IntegrationError
from ..config import settings
from ..db import (
    candidate_applications,
    candidate_communications,
    candidates,
    email_attachments,
    email_messages,
    inbox_sync_settings,
    ingestion_jobs,
    jobs,
    utcnow,
)
from ..repo import normalize_email
from .mail import AttachmentMeta, MailMessage, MailProvider
from .resume import SUPPORTED_EXTENSIONS, ResumeParseError, extract_text, parse_resume
from .storage import content_hash, storage

log = logging.getLogger("recruiter.ingestion")

RESUME_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/octet-stream",
    "application/x-pdf",
}
MAX_ATTEMPTS = settings.sync_max_attempts


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff, capped at 6 hours."""
    return min(6 * 3600, 60 * (2 ** max(0, attempt - 1)))


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_supported_resume(attachment: AttachmentMeta) -> bool:
    ext = _ext(attachment.filename)
    if ext not in SUPPORTED_EXTENSIONS:
        return False
    if attachment.mime_type and attachment.mime_type not in RESUME_MIME_TYPES:
        # Some clients send odd MIME types; trust the extension for known docs.
        if attachment.mime_type.startswith("image/"):
            return False
    return True


# ------------------------------------------------------- application subjects
_REPLY_SUBJECT = re.compile(r"^(?:(?:re|fwd|fw)\s*:\s*)+", re.IGNORECASE)
_SUBJECT_SEPARATOR = re.compile(r"^[\s\-–—|:]+")


def normalize_subject_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def subject_matches_rule(subject: str | None, prefix: str | None) -> bool:
    """True when the (reply-stripped) subject starts with the job's headline."""
    if not subject or not prefix:
        return False
    stripped = subject.strip()
    stripped = _REPLY_SUBJECT.sub("", stripped)
    stripped = _SUBJECT_SEPARATOR.sub("", stripped)
    return stripped.casefold().startswith(prefix.strip().casefold())


def build_subject_query(window: str, prefix: str) -> str:
    """Gmail search targeting a job headline, restricted to attachment emails."""
    safe_prefix = prefix.replace('"', "").strip()
    return f'{window} subject:"{safe_prefix}" has:attachment'.strip()


async def application_subject_rules(
    session: AsyncSession, *, workspace_id: str, limit: int = 25
) -> list[dict[str, Any]]:
    """Open jobs that declare a subject headline, longest prefix first."""
    rows = (
        await session.execute(
            sa.select(jobs.c.id, jobs.c.application_subject_prefix)
            .where(
                jobs.c.workspace_id == workspace_id,
                jobs.c.status != "closed",
                jobs.c.application_subject_prefix.is_not(None),
                sa.func.length(sa.func.trim(jobs.c.application_subject_prefix)) > 0,
            )
            .order_by(sa.func.length(jobs.c.application_subject_prefix).desc())
            .limit(limit)
        )
    ).mappings().all()
    rules = []
    for row in rows:
        prefix = normalize_subject_prefix(row["application_subject_prefix"])
        if prefix:
            rules.append({"job_id": row["id"], "prefix": prefix})
    return rules


# ----------------------------------------------------------------- settings
async def ensure_sync_settings(
    session: AsyncSession, *, workspace_id: str, account_id: str, interval_minutes: int | None = None
) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.select(inbox_sync_settings).where(inbox_sync_settings.c.connected_account_id == account_id)
        )
    ).mappings().first()
    if row:
        return dict(row)
    now = utcnow()
    interval = interval_minutes or settings.sync_default_interval_minutes
    record = {
        "id": db.new_id(),
        "workspace_id": workspace_id,
        "connected_account_id": account_id,
        "enabled": True,
        "interval_minutes": interval,
        "is_custom_interval": interval not in {15, 30, 60, 180, 360, 1440},
        "query": "newer_than:2d",
        "last_synced_at": None,
        "next_sync_at": now,
        "last_sync_status": None,
        "last_sync_error": None,
        "consecutive_failures": 0,
        "history_cursor": None,
        "overlap_minutes": settings.sync_overlap_minutes,
        "created_at": now,
        "updated_at": now,
    }
    # The page can issue this concurrently with itself (e.g. a double effect or
    # a second tab), so let the unique constraint arbitrate and re-read instead
    # of failing the request.
    await session.execute(
        pg_insert(inbox_sync_settings)
        .values(**record)
        .on_conflict_do_nothing(index_elements=["connected_account_id"])
    )
    await session.commit()
    row = (
        await session.execute(
            sa.select(inbox_sync_settings).where(inbox_sync_settings.c.connected_account_id == account_id)
        )
    ).mappings().first()
    return dict(row) if row else record


async def mark_sync_success(session: AsyncSession, settings_row: dict[str, Any], *, stats: dict[str, Any]) -> None:
    now = utcnow()
    await session.execute(
        sa.update(inbox_sync_settings)
        .where(inbox_sync_settings.c.id == settings_row["id"])
        .values(
            last_synced_at=now,
            next_sync_at=now + timedelta(minutes=settings_row["interval_minutes"]),
            last_sync_status="success",
            last_sync_error=None,
            consecutive_failures=0,
            updated_at=now,
        )
    )
    await session.commit()


async def mark_sync_failure(
    session: AsyncSession, settings_row: dict[str, Any], *, error: str, stats: dict[str, Any] | None = None
) -> None:
    now = utcnow()
    failures = int(settings_row.get("consecutive_failures") or 0) + 1
    delay = backoff_seconds(failures)
    await session.execute(
        sa.update(inbox_sync_settings)
        .where(inbox_sync_settings.c.id == settings_row["id"])
        .values(
            last_sync_status="failed",
            last_sync_error=error[:800],
            consecutive_failures=failures,
            next_sync_at=now + timedelta(seconds=max(delay, 60)),
            updated_at=now,
        )
    )
    await session.commit()


# --------------------------------------------------------------------- jobs
async def active_job_for_account(session: AsyncSession, account_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            sa.select(ingestion_jobs)
            .where(
                ingestion_jobs.c.connected_account_id == account_id,
                ingestion_jobs.c.status.in_(("queued", "running")),
            )
            .order_by(ingestion_jobs.c.created_at.desc())
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


async def active_job_account_ids(session: AsyncSession, account_ids: list[str]) -> set[str]:
    """Batch equivalent of :func:`active_job_for_account` (one query, no N+1)."""
    if not account_ids:
        return set()
    rows = (
        await session.execute(
            sa.select(ingestion_jobs.c.connected_account_id)
            .where(
                ingestion_jobs.c.connected_account_id.in_(account_ids),
                ingestion_jobs.c.status.in_(("queued", "running")),
            )
            .distinct()
        )
    ).all()
    return {row[0] for row in rows}


async def enqueue_job(
    session: AsyncSession,
    *,
    workspace_id: str,
    account_id: str,
    trigger: str = "cron",
    check_active: bool = True,
) -> dict[str, Any]:
    if check_active:
        existing = await active_job_for_account(session, account_id)
        if existing:
            return existing
    now = utcnow()
    record = {
        "id": db.new_id(),
        "workspace_id": workspace_id,
        "connected_account_id": account_id,
        "status": "queued",
        "trigger": trigger,
        "attempts": 0,
        "max_attempts": MAX_ATTEMPTS,
        "locked_by": None,
        "locked_at": None,
        "started_at": None,
        "completed_at": None,
        "next_retry_at": None,
        "error_message": None,
        "stats": {},
        "created_at": now,
    }
    await session.execute(sa.insert(ingestion_jobs).values(**record))
    await session.commit()
    return record


async def claim_job(session: AsyncSession, job_id: str, worker_id: str) -> bool:
    now = utcnow()
    result = await session.execute(
        sa.update(ingestion_jobs)
        .where(
            ingestion_jobs.c.id == job_id,
            ingestion_jobs.c.status.in_(("queued", "retrying")),
        )
        .values(
            status="running",
            locked_by=worker_id,
            locked_at=now,
            started_at=now,
            attempts=ingestion_jobs.c.attempts + 1,
        )
    )
    await session.commit()
    return bool(result.rowcount)


async def finish_job(
    session: AsyncSession, job_id: str, *, status: str, stats: dict[str, Any], error: str | None = None
) -> None:
    now = utcnow()
    await session.execute(
        sa.update(ingestion_jobs)
        .where(ingestion_jobs.c.id == job_id)
        .values(status=status, stats=stats, error_message=error[:800] if error else None, completed_at=now)
    )
    await session.commit()


async def schedule_retry(
    session: AsyncSession, job_id: str, *, attempts: int, error: str, stats: dict[str, Any]
) -> bool:
    now = utcnow()
    if attempts >= MAX_ATTEMPTS:
        await finish_job(session, job_id, status="failed", stats=stats, error=error)
        return False
    delay = backoff_seconds(attempts)
    await session.execute(
        sa.update(ingestion_jobs)
        .where(ingestion_jobs.c.id == job_id)
        .values(
            status="retrying",
            next_retry_at=now + timedelta(seconds=delay),
            error_message=error[:800],
            stats=stats,
            locked_by=None,
            locked_at=None,
        )
    )
    await session.commit()
    return True


# ------------------------------------------------------------------- stages
async def _store_email(session: AsyncSession, *, workspace_id: str, account_id: str, message: MailMessage) -> tuple[str, bool]:
    existing = (
        await session.execute(
            sa.select(email_messages.c.id, email_messages.c.processing_status).where(
                email_messages.c.connected_account_id == account_id,
                email_messages.c.provider_message_id == message.provider_message_id,
            )
        )
    ).mappings().first()
    if existing:
        return existing["id"], False

    message_id = db.new_id()
    await session.execute(
        sa.insert(email_messages).values(
            id=message_id,
            workspace_id=workspace_id,
            connected_account_id=account_id,
            provider_message_id=message.provider_message_id,
            thread_id=message.thread_id,
            sender_email=message.sender_email,
            sender_name=message.sender_name,
            recipients=message.recipients,
            subject=message.subject,
            snippet=message.snippet,
            received_at=message.received_at or utcnow(),
            has_attachments=bool(message.attachments),
            processing_status="pending",
            headers=message.headers,
            created_at=utcnow(),
        )
    )
    await session.commit()
    return message_id, True


async def _upsert_candidate(
    session: AsyncSession,
    *,
    workspace_id: str,
    parsed,
    text: str,
    storage_path: str,
    filename: str,
    sha: str,
    source: str = "gmail",
) -> tuple[str, bool]:
    data = parsed.data
    email_norm = normalize_email(data.email)
    row = None
    if email_norm:
        row = (
            await session.execute(
                sa.select(candidates).where(
                    candidates.c.workspace_id == workspace_id,
                    candidates.c.email_normalized == email_norm,
                )
            )
        ).mappings().first()
    if row is None and data.full_name:
        row = (
            await session.execute(
                sa.select(candidates).where(
                    candidates.c.workspace_id == workspace_id,
                    candidates.c.full_name == data.full_name,
                    candidates.c.phone == data.phone if data.phone else candidates.c.phone.is_(None),
                )
            )
        ).mappings().first()

    now = utcnow()
    values = {
        "full_name": data.full_name or (email_norm.split("@")[0].title() if email_norm else "Unknown candidate"),
        "email": data.email,
        "email_normalized": email_norm,
        "phone": data.phone,
        "location": data.location,
        "preferred_location": data.preferred_location,
        "experience_years": data.experience_years,
        "current_role": data.current_role,
        "current_company": data.current_company,
        "previous_companies": data.previous_companies,
        "education": [e.model_dump() for e in data.education],
        "university": data.university,
        "graduation_year": data.graduation_year,
        "skills": data.skills,
        "programming_languages": data.programming_languages,
        "frameworks": data.frameworks,
        "tools": data.tools,
        "projects": [p.model_dump() for p in data.projects],
        "summary": data.summary,
        "github_url": data.github_url,
        "leetcode_url": data.leetcode_url,
        "linkedin_url": data.linkedin_url,
        "portfolio_url": data.portfolio_url,
        "resume_storage_path": storage_path,
        "resume_filename": filename,
        "resume_text": text[:120000],
        "resume_sha256": sha,
        "extraction_method": parsed.method,
        "extraction_confidence": parsed.confidence,
        "extraction": {"data": data.model_dump(), "warnings": parsed.warnings},
        "updated_at": now,
    }

    if row:
        candidate_id = row["id"]
        if row.get("resume_sha256") == sha:
            # Same resume re-sent: refresh metadata only, keep history intact.
            await session.execute(
                sa.update(candidates)
                .where(candidates.c.id == candidate_id)
                .values(updated_at=now)
            )
            await session.commit()
            return candidate_id, False
        await session.execute(
            sa.update(candidates).where(candidates.c.id == candidate_id).values(**values)
        )
        await session.commit()
        return candidate_id, False

    candidate_id = db.new_id()
    await session.execute(
        sa.insert(candidates).values(
            id=candidate_id,
            workspace_id=workspace_id,
            pipeline_status="APPLIED",
            source=source,
            owner_id=None,
            created_at=now,
            **values,
        )
    )
    await session.commit()
    return candidate_id, True


async def process_attachment(
    session: AsyncSession,
    *,
    workspace_id: str,
    message_id: str,
    message: MailMessage,
    attachment: AttachmentMeta,
    provider: MailProvider,
    stats: dict[str, Any],
    job_id: str | None = None,
) -> None:
    ext = _ext(attachment.filename)
    if not is_supported_resume(attachment):
        await session.execute(
            sa.insert(email_attachments).values(
                id=db.new_id(),
                workspace_id=workspace_id,
                email_message_id=message_id,
                provider_attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                status="skipped",
                error_message=f"Unsupported attachment type (.{ext or 'unknown'})",
                created_at=utcnow(),
            )
        )
        await session.commit()
        stats["attachments_skipped"] += 1
        return

    if attachment.size_bytes and attachment.size_bytes > settings.sync_max_resume_bytes:
        await session.execute(
            sa.insert(email_attachments).values(
                id=db.new_id(),
                workspace_id=workspace_id,
                email_message_id=message_id,
                provider_attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                status="skipped",
                error_message="Attachment exceeds the configured size limit",
                created_at=utcnow(),
            )
        )
        await session.commit()
        stats["attachments_skipped"] += 1
        return

    existing = (
        await session.execute(
            sa.select(email_attachments).where(
                email_attachments.c.email_message_id == message_id,
                email_attachments.c.provider_attachment_id == attachment.attachment_id,
            )
        )
    ).mappings().first()
    if existing and existing["status"] in {"parsed", "downloaded"}:
        stats["attachments_duplicate"] += 1
        return

    attachment_id = existing["id"] if existing else db.new_id()
    if not existing:
        await session.execute(
            sa.insert(email_attachments).values(
                id=attachment_id,
                workspace_id=workspace_id,
                email_message_id=message_id,
                provider_attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                status="pending",
                created_at=utcnow(),
            )
        )
        await session.commit()

    try:
        blob = await provider.download_attachment(message, attachment)
        if not blob:
            raise ResumeParseError("Attachment downloaded with zero bytes")
        if len(blob) > settings.sync_max_resume_bytes:
            raise ResumeParseError("Attachment exceeds the configured size limit")
        sha = content_hash(blob)
        path = f"{workspace_id}/{sha[:20]}.{ext}"
        text, text_method = extract_text(blob, attachment.filename)
        if text_method == "pdf_no_text":
            raise ResumeParseError("PDF has no embedded text (scanned image); OCR is required")
        parsed = await parse_resume(text, filename=attachment.filename, email_hint=message.sender_email)
        stored_path = await storage.put(path, blob, attachment.mime_type or "application/octet-stream")
        candidate_id, created = await _upsert_candidate(
            session,
            workspace_id=workspace_id,
            parsed=parsed,
            text=text,
            storage_path=stored_path,
            filename=attachment.filename,
            sha=sha,
            source="gmail",
        )
        await session.execute(
            sa.update(email_attachments)
            .where(email_attachments.c.id == attachment_id)
            .values(status="parsed", storage_path=stored_path, sha256=sha, size_bytes=len(blob))
        )
        # Link the application to the job whose subject rule matched (if any).
        linked = (
            await session.execute(
                sa.select(candidate_applications.c.id, candidate_applications.c.job_id).where(
                    candidate_applications.c.candidate_id == candidate_id,
                    candidate_applications.c.email_message_id == message_id,
                )
            )
        ).mappings().first()
        if not linked:
            await session.execute(
                sa.insert(candidate_applications).values(
                    id=db.new_id(),
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    email_message_id=message_id,
                    job_id=job_id,
                    application_date=message.received_at or utcnow(),
                    created_at=utcnow(),
                )
            )
            if job_id:
                stats["applications_linked"] += 1
        elif job_id and not linked["job_id"]:
            await session.execute(
                sa.update(candidate_applications)
                .where(candidate_applications.c.id == linked["id"])
                .values(job_id=job_id)
            )
            stats["applications_linked"] += 1
        # Record the inbound email in the communication history.
        already_logged = (
            await session.execute(
                sa.select(candidate_communications.c.id).where(
                    candidate_communications.c.provider_message_id == message.provider_message_id,
                    candidate_communications.c.candidate_id == candidate_id,
                )
            )
        ).scalar_one_or_none()
        if not already_logged:
            await session.execute(
                sa.insert(candidate_communications).values(
                    id=db.new_id(),
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    recruiter_id=None,
                    channel="email",
                    direction="inbound",
                    kind="application",
                    subject=message.subject,
                    content=message.body_text[:20000],
                    provider="gmail",
                    provider_message_id=message.provider_message_id,
                    status="received",
                    metadata={"job_id": job_id} if job_id else {},
                    created_at=message.received_at or utcnow(),
                )
            )
        await session.commit()
        stats["resumes_parsed"] += 1
        stats["candidates_created" if created else "candidates_updated"] += 1
        if parsed.warnings:
            stats.setdefault("warnings", []).append(parsed.warnings[0])
    except (IntegrationError, ResumeParseError, ValueError, OSError) as exc:
        log.warning("attachment %s failed: %s", attachment.filename, exc)
        await session.execute(
            sa.update(email_attachments)
            .where(email_attachments.c.id == attachment_id)
            .values(status="failed", error_message=str(exc)[:500])
        )
        await session.commit()
        stats["attachments_failed"] += 1


async def process_message(
    session: AsyncSession,
    *,
    workspace_id: str,
    account_id: str,
    message: MailMessage,
    provider: MailProvider,
    stats: dict[str, Any],
    job_id: str | None = None,
) -> None:
    message_id, is_new = await _store_email(
        session, workspace_id=workspace_id, account_id=account_id, message=message
    )
    if not is_new:
        stats["emails_duplicate"] += 1
        if job_id:
            linked_now = await session.execute(
                sa.update(candidate_applications)
                .where(
                    candidate_applications.c.email_message_id == message_id,
                    candidate_applications.c.job_id.is_(None),
                )
                .values(job_id=job_id)
            )
            if linked_now.rowcount:
                stats["applications_linked"] += linked_now.rowcount
                await session.commit()
        return

    supported = [a for a in message.attachments if is_supported_resume(a)]
    if not supported:
        await session.execute(
            sa.update(email_messages)
            .where(email_messages.c.id == message_id)
            .values(
                processing_status="skipped",
                skip_reason="No supported resume attachment (pdf/docx/doc)",
            )
        )
        await session.commit()
        stats["emails_skipped"] += 1
        return

    await session.execute(
        sa.update(email_messages)
        .where(email_messages.c.id == message_id)
        .values(processing_status="processing")
    )
    await session.commit()

    stats["emails_processed"] += 1
    for attachment in message.attachments:
        await process_attachment(
            session,
            workspace_id=workspace_id,
            message_id=message_id,
            message=message,
            attachment=attachment,
            provider=provider,
            stats=stats,
            job_id=job_id,
        )

    parsed_any = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(email_attachments)
            .where(
                email_attachments.c.email_message_id == message_id,
                email_attachments.c.status == "parsed",
            )
        )
    ).scalar_one()
    status = "processed" if parsed_any else "failed"
    await session.execute(
        sa.update(email_messages)
        .where(email_messages.c.id == message_id)
        .values(processing_status=status)
    )
    await session.commit()


# ------------------------------------------------------------------ runner
async def run_job(
    session: AsyncSession,
    *,
    job: dict[str, Any],
    worker_id: str,
    provider: MailProvider | None = None,
) -> dict[str, Any]:
    """Delegate to the LangGraph ingestion workflow (single source of truth)."""
    from ..graphs.ingestion_graph import run_ingestion

    return await run_ingestion(session=session, job=job, worker_id=worker_id, provider=provider)


async def due_accounts(session: AsyncSession, *, limit: int = 20) -> list[dict[str, Any]]:
    now = utcnow()
    stmt = (
        sa.select(inbox_sync_settings)
        .where(
            inbox_sync_settings.c.enabled.is_(True),
            sa.or_(inbox_sync_settings.c.next_sync_at.is_(None), inbox_sync_settings.c.next_sync_at <= now),
        )
        .order_by(inbox_sync_settings.c.next_sync_at.asc())
        .limit(limit)
    )
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]


async def due_jobs(session: AsyncSession, *, limit: int = 10) -> list[dict[str, Any]]:
    now = utcnow()
    stmt = (
        sa.select(ingestion_jobs)
        .where(
            sa.or_(
                ingestion_jobs.c.status == "queued",
                sa.and_(
                    ingestion_jobs.c.status == "retrying",
                    sa.or_(ingestion_jobs.c.next_retry_at.is_(None), ingestion_jobs.c.next_retry_at <= now),
                ),
            )
        )
        .order_by(ingestion_jobs.c.created_at.asc())
        .limit(limit)
    )
    return [dict(r) for r in (await session.execute(stmt)).mappings().all()]
