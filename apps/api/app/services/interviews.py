"""Interview scheduling: availability → approval → Teams/Meet → Gmail invite.

Guarantees:

* Gmail is the only email channel. Microsoft Teams and Google Meet are the
  supported meeting providers; the workspace/recruiter picks one.
* a conflict is detected against interviews this product created before anything
  external is created;
* nothing external happens until the recruiter approves (unless the caller
  explicitly passes ``approve=True``);
* retries are idempotent through ``interviews.idempotency_key`` (unique) and a
  pre-flight lookup;
* the real join URL (and Google Calendar event, when Google is connected) is
  created **before** the invitation is drafted, and the exact join URL is stored
  on the interview row;
* the invitation email is queued as a *pending action* so a recruiter approves
  the send. The meeting is never faked: a missing join URL is an error.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..composio import IntegrationError
from ..db import candidates, interviews, pending_actions, utcnow, workspace_members
from ..repo import (
    create_pending_action,
    find_account,
    get_candidate,
    log_activity,
    meeting_provider_for,
    normalize_meeting_provider,
    set_candidate_status,
)
from ..schemas import ScheduleRequest
from .calendar import (
    GoogleMeetProvider,
    MeetingLink,
    TeamsMeetingProvider,
    TimeSlot,
    generate_alternatives,
    local_label,
)
from .communications import draft_email, format_when, queue_email

log = logging.getLogger("recruiter.interviews")


def _parse_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _idempotency_key(workspace_id: str, candidate_id: str, start: datetime, recruiter_id: str | None) -> str:
    raw = f"{workspace_id}|{candidate_id}|{start.astimezone(timezone.utc).isoformat()}|{recruiter_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


async def _teams_provider(session: AsyncSession, workspace_id: str) -> TeamsMeetingProvider:
    account = await find_account(session, workspace_id, "microsoft_teams")
    return TeamsMeetingProvider(account)


async def _google_meet_provider(session: AsyncSession, workspace_id: str) -> GoogleMeetProvider:
    calendar_account = await find_account(session, workspace_id, "google_calendar")
    meet_account = await find_account(session, workspace_id, "google_meet")
    return GoogleMeetProvider(calendar_account, meet_account)


async def _recruiter_email(session: AsyncSession, workspace_id: str, recruiter_id: str | None) -> str | None:
    row = (
        await session.execute(
            sa.select(workspace_members.c.email).where(
                workspace_members.c.workspace_id == workspace_id,
                sa.or_(
                    workspace_members.c.user_id == (recruiter_id or ""),
                    workspace_members.c.role == "owner",
                ),
            )
        )
    ).first()
    return row[0] if row else None


async def availability(
    session: AsyncSession,
    *,
    workspace_id: str,
    start: datetime | None,
    duration_minutes: int,
    timezone_name: str,
) -> dict[str, Any]:
    """Check a requested slot against interviews created by this workspace.

    External calendar availability is intentionally not inferred: the Teams
    toolkit does not expose the recruiter's Outlook calendar, so claiming a slot
    is free would be fabricated. The response labels the conflict source.
    """
    provider = await _teams_provider(session, workspace_id)
    google = await _google_meet_provider(session, workspace_id)
    now = utcnow()
    window_start = start or (now + timedelta(hours=1))
    window_end = window_start + timedelta(minutes=max(30, duration_minutes) + 60)

    local_rows = (
        await session.execute(
            sa.select(interviews.c.start_time, interviews.c.end_time, interviews.c.status).where(
                interviews.c.workspace_id == workspace_id,
                interviews.c.status.in_(("scheduled", "rescheduled")),
            )
        )
    ).mappings().all()
    local_slots = [
        TimeSlot(_parse_dt(row["start_time"]), _parse_dt(row["end_time"]))
        for row in local_rows
        if row["start_time"] and row["end_time"]
    ]

    requested = TimeSlot(window_start, window_start + timedelta(minutes=duration_minutes))
    conflicts = [
        {"start": slot.start.isoformat(), "end": slot.end.isoformat(), "source": "local"}
        for slot in local_slots
        if requested.overlaps(slot)
    ]

    alternatives = generate_alternatives(
        after=window_start,
        duration_minutes=duration_minutes,
        busy=[],
        existing=local_slots,
        timezone_name=timezone_name,
    )
    return {
        "requested_start": window_start,
        "requested_end": requested.end,
        "available": not conflicts,
        "conflicts": conflicts,
        "alternatives": [
            {"start": slot.start, "end": slot.end, "label": local_label(slot, timezone_name)}
            for slot in alternatives
        ],
        # ``calendar_connected`` is kept for API compatibility and now means
        # "a meeting provider is connected".
        "calendar_connected": provider.connected or google.connected,
        "teams_connected": provider.connected,
        "google_meet_connected": google.connected,
        "calendar_error": None,
        "timezone": timezone_name,
        "availability_source": "local_interviews",
    }


async def propose(
    session: AsyncSession,
    *,
    workspace_id: str,
    recruiter_id: str | None,
    candidate: dict[str, Any],
    request: ScheduleRequest,
) -> dict[str, Any]:
    """Create an approval-gated scheduling action (or execute it when approved)."""
    timezone_name = request.timezone or "Asia/Kolkata"
    start = _parse_dt(request.start)
    if start is None:
        raise ValueError("A start time is required")
    duration = request.duration_minutes
    provider = (
        normalize_meeting_provider(request.provider)
        if request.provider
        else await meeting_provider_for(session, workspace_id)
    )

    check = await availability(
        session, workspace_id=workspace_id, start=start, duration_minutes=duration, timezone_name=timezone_name
    )
    if not check["available"]:
        return {
            "status": "needs_new_slot",
            "reason": "The requested time conflicts with an existing interview.",
            "conflicts": check["conflicts"],
            "alternatives": check["alternatives"],
            "timezone": timezone_name,
        }

    start_utc = start.astimezone(timezone.utc)
    key = request.idempotency_key or _idempotency_key(workspace_id, candidate["id"], start_utc, recruiter_id)
    existing = (
        await session.execute(sa.select(interviews).where(interviews.c.idempotency_key == key))
    ).mappings().first()
    if existing:
        return {"status": "already_scheduled", "interview": dict(existing)}

    payload = {
        "candidate_id": candidate["id"],
        "candidate_name": candidate.get("full_name"),
        "start": start_utc.isoformat(),
        "duration_minutes": duration,
        "timezone": timezone_name,
        "provider": provider,
        "title": request.title,
        "notes": request.notes,
        "idempotency_key": key,
    }
    title = (
        request.title
        or f"Interview: {candidate.get('full_name') or 'Candidate'}"
        + (f" — {request.notes}" if request.notes and len(request.notes) < 40 else "")
    )

    if request.approve:
        return await execute_schedule(
            session,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            payload=payload,
        )

    action = await create_pending_action(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        kind="schedule_interview",
        title=title[:120],
        payload=payload,
    )
    await log_activity(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        action="interview.proposed",
        entity_type="candidate",
        entity_id=candidate["id"],
        metadata={"start": payload["start"], "provider": provider},
    )
    await session.commit()
    return {"status": "pending_approval", "action": action, "payload": payload, "timezone": timezone_name}


async def _queue_confirmation(
    session: AsyncSession,
    *,
    workspace_id: str,
    recruiter_id: str | None,
    candidate: dict[str, Any],
    interview_id: str,
    provider: str,
    start: datetime,
    timezone_name: str,
    join_url: str | None,
    notes: str | None,
    kind: str = "interview_confirmation",
) -> tuple[dict[str, Any], dict[str, Any]]:
    recruiter_email = await _recruiter_email(session, workspace_id, recruiter_id)
    interview_view = {
        "provider": provider,
        "start_time": start.isoformat(),
        "start_label": format_when(start, timezone_name),
        "meeting_url": join_url,
        "join_url": join_url,
    }
    subject, body, method = await draft_email(
        kind=kind,
        candidate=candidate,
        recruiter_name=recruiter_email or "Recruiting team",
        interview=interview_view,
        notes=notes,
    )
    comm = await queue_email(
        session,
        workspace_id=workspace_id,
        candidate=candidate,
        recruiter_id=recruiter_id,
        kind=kind,
        subject=subject,
        body=body,
        metadata={"interview_id": interview_id, "provider": provider, "auto_from_scheduling": True},
        method=method,
    )
    action = await create_pending_action(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        kind="send_email",
        title=f"Send {'reschedule' if kind == 'reschedule' else 'interview confirmation'} to "
        f"{candidate.get('full_name') or 'candidate'}",
        payload={
            "communication_id": comm["id"],
            "candidate_id": candidate["id"],
            "subject": subject,
            "interview_id": interview_id,
        },
    )
    return comm, action


async def execute_schedule(
    session: AsyncSession,
    *,
    workspace_id: str,
    recruiter_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    candidate = await get_candidate(session, workspace_id, payload["candidate_id"])
    if not candidate:
        raise ValueError("Candidate not found")

    start = _parse_dt(payload["start"])
    assert start is not None
    duration = int(payload.get("duration_minutes") or 30)
    end = start + timedelta(minutes=duration)
    timezone_name = payload.get("timezone") or "Asia/Kolkata"
    provider = normalize_meeting_provider(payload.get("provider"))
    key = payload.get("idempotency_key") or _idempotency_key(
        workspace_id, candidate["id"], start, recruiter_id
    )

    existing = (
        await session.execute(sa.select(interviews).where(interviews.c.idempotency_key == key))
    ).mappings().first()
    # Only short-circuit when the slot is already booked with the *same*
    # provider. If the recruiter switched providers (e.g. Teams -> Google Meet)
    # we must rebuild the meeting instead of handing back the stale link.
    if existing and existing.get("provider") == provider:
        return {"status": "already_scheduled", "interview": dict(existing), "meeting_url": existing.get("meeting_url")}

    subject = payload.get("title") or f"Interview: {candidate.get('full_name') or 'Candidate'}"
    meeting: MeetingLink | None = None

    if provider == "microsoft_teams":
        teams = await _teams_provider(session, workspace_id)
        if not teams.connected:
            raise IntegrationError(
                "Microsoft Teams is not connected. Connect it from Settings → Integrations "
                "before scheduling interviews.",
                provider="microsoft_teams",
                code="not_connected",
            )
        meeting = await teams.create_meeting(
            subject=subject,
            start=start,
            end=end,
            external_id=key,
        )
    elif provider == "google_meet":
        google = await _google_meet_provider(session, workspace_id)
        if not google.connected:
            raise IntegrationError(
                "Google Meet is not connected. Connect Google Calendar or Google Meet from "
                "Settings → Integrations before scheduling interviews.",
                provider="google_meet",
                code="not_connected",
            )
        meeting = await google.create_meeting(
            subject=subject,
            start=start,
            end=end,
            external_id=key,
            timezone_name=timezone_name,
            attendee_email=candidate.get("email"),
            description=payload.get("notes"),
        )

    join_url = meeting.join_url if meeting else None
    if provider != "none" and not join_url:
        # Never fall back to a fabricated room: an interview without a real
        # link is worse than an honest failure the recruiter can act on.
        raise IntegrationError(
            "The meeting provider did not return a join URL; the interview was not scheduled.",
            provider=provider,
            code="bad_response",
        )

    invite_status = "queued" if candidate.get("email") else "no_candidate_email"
    now = utcnow()
    values = {
        "provider": provider,
        "calendar_provider": meeting.calendar_provider if meeting else "none",
        "calendar_id": (meeting.calendar_id or "") if meeting else "",
        "calendar_event_id": meeting.calendar_event_id if meeting else None,
        "calendar_html_link": meeting.calendar_html_link if meeting else None,
        "meeting_id": meeting.meeting_id if meeting else None,
        "meeting_url": join_url,
        "start_time": start,
        "end_time": end,
        "timezone": timezone_name,
        "status": "scheduled",
        "invite_status": invite_status,
        "attendees": [{"email": email} for email in (candidate.get("email"),) if email],
        "notes": payload.get("notes"),
        "updated_at": now,
    }
    if existing:
        # Provider switch on an already-booked slot: update the existing row in
        # place so the idempotency key stays unique and downstream references
        # (confirmation email, pending action) keep the same interview id.
        interview_id = existing["id"]
        await session.execute(
            sa.update(interviews).where(interviews.c.id == interview_id).values(**values)
        )
    else:
        interview_id = uuid.uuid4().hex
        await session.execute(
            sa.insert(interviews).values(
                id=interview_id,
                workspace_id=workspace_id,
                candidate_id=candidate["id"],
                recruiter_id=recruiter_id,
                job_id=None,
                idempotency_key=key,
                created_at=now,
                **values,
            )
        )
    await session.commit()

    await set_candidate_status(
        session,
        workspace_id=workspace_id,
        candidate_id=candidate["id"],
        to_status="INTERVIEW_SCHEDULED",
        changed_by=recruiter_id,
        reason="Interview scheduled",
    )

    comm: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    if candidate.get("email"):
        comm, action = await _queue_confirmation(
            session,
            workspace_id=workspace_id,
            recruiter_id=recruiter_id,
            candidate=candidate,
            interview_id=interview_id,
            provider=provider,
            start=start,
            timezone_name=timezone_name,
            join_url=join_url,
            notes=payload.get("notes"),
        )

    await log_activity(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        action="interview.scheduled",
        entity_type="candidate",
        entity_id=candidate["id"],
        metadata={
            "provider": provider,
            "start": start.isoformat(),
            "meeting_url": bool(join_url),
            "meeting_id": meeting.meeting_id if meeting else None,
            "calendar_event_id": meeting.calendar_event_id if meeting else None,
        },
    )
    await session.commit()

    return {
        "status": "scheduled",
        "interview_id": interview_id,
        "provider": provider,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "timezone": timezone_name,
        "meeting_url": join_url,
        "meeting_id": meeting.meeting_id if meeting else None,
        "calendar_provider": meeting.calendar_provider if meeting else "none",
        "calendar_event_id": meeting.calendar_event_id if meeting else None,
        "calendar_html_link": meeting.calendar_html_link if meeting else None,
        "invite_status": invite_status,
        "confirmation_email": comm,
        "confirmation_action": action,
        "warnings": [],
    }


async def cancel_interview(
    session: AsyncSession, *, workspace_id: str, interview_id: str, recruiter_id: str | None, reason: str | None = None
) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.select(interviews).where(
                interviews.c.id == interview_id, interviews.c.workspace_id == workspace_id
            )
        )
    ).mappings().first()
    if not row:
        raise ValueError("Interview not found")
    if row["status"] == "cancelled":
        return {"status": "cancelled", "already": True}

    if row["meeting_id"] or row["calendar_event_id"]:
        try:
            if row["provider"] == "microsoft_teams" and row["meeting_id"]:
                teams = await _teams_provider(session, workspace_id)
                if teams.connected:
                    await teams.delete_meeting(row["meeting_id"])
            elif row["provider"] == "google_meet":
                google = await _google_meet_provider(session, workspace_id)
                if google.connected:
                    await google.delete_meeting(row["calendar_event_id"] or row["meeting_id"])
        except IntegrationError as exc:
            log.warning("Meeting delete failed for interview %s: %s", interview_id, exc)
            raise

    await session.execute(
        sa.update(interviews)
        .where(interviews.c.id == interview_id)
        .values(status="cancelled", updated_at=utcnow())
    )
    await session.commit()
    await log_activity(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        action="interview.cancelled",
        entity_type="candidate",
        entity_id=row["candidate_id"],
        metadata={"interview_id": interview_id, "reason": reason},
    )
    await session.commit()
    return {"status": "cancelled", "interview_id": interview_id}


async def reschedule_interview(
    session: AsyncSession,
    *,
    workspace_id: str,
    interview_id: str,
    new_start: datetime,
    recruiter_id: str | None,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.select(interviews).where(
                interviews.c.id == interview_id, interviews.c.workspace_id == workspace_id
            )
        )
    ).mappings().first()
    if not row:
        raise ValueError("Interview not found")

    candidate = await get_candidate(session, workspace_id, row["candidate_id"])
    if not candidate:
        raise ValueError("Candidate not found")

    duration = duration_minutes or int((row["end_time"] - row["start_time"]).total_seconds() // 60) or 30
    end = new_start + timedelta(minutes=duration)
    timezone_name = row["timezone"] or "Asia/Kolkata"
    subject = f"Interview: {candidate.get('full_name') or 'Candidate'}"

    join_url = row["meeting_url"]
    meeting_id = row["meeting_id"]
    calendar_event_id = row["calendar_event_id"]
    calendar_html_link = row["calendar_html_link"]

    if row["provider"] == "microsoft_teams":
        teams = await _teams_provider(session, workspace_id)
        if not teams.connected:
            raise IntegrationError(
                "Microsoft Teams is not connected", provider="microsoft_teams", code="not_connected"
            )
        updated: MeetingLink | None = None
        if meeting_id:
            try:
                updated = await teams.update_meeting(
                    meeting_id=meeting_id, subject=subject, start=new_start, end=end
                )
            except IntegrationError as exc:
                log.warning("Teams meeting update failed, creating a replacement: %s", exc)
        if updated is None:
            replacement_key = _idempotency_key(
                workspace_id, candidate["id"], new_start, recruiter_id
            )
            updated = await teams.create_meeting(
                subject=subject, start=new_start, end=end, external_id=replacement_key
            )
            if meeting_id:
                try:
                    await teams.delete_meeting(meeting_id)
                except IntegrationError:
                    log.warning("Old Teams meeting %s could not be deleted", meeting_id)
        join_url = updated.join_url or join_url
        meeting_id = updated.meeting_id or meeting_id
    elif row["provider"] == "google_meet":
        google = await _google_meet_provider(session, workspace_id)
        if not google.connected:
            raise IntegrationError(
                "Google Meet is not connected", provider="google_meet", code="not_connected"
            )
        updated = await google.update_meeting(
            meeting_id=calendar_event_id or meeting_id or "",
            subject=subject,
            start=new_start,
            end=end,
            timezone_name=timezone_name,
            join_url=join_url,
        )
        join_url = updated.join_url or join_url
        meeting_id = updated.meeting_id or meeting_id
        calendar_event_id = updated.calendar_event_id or calendar_event_id
        calendar_html_link = updated.calendar_html_link or calendar_html_link

    await session.execute(
        sa.update(interviews)
        .where(interviews.c.id == interview_id)
        .values(
            start_time=new_start,
            end_time=end,
            status="rescheduled",
            meeting_id=meeting_id,
            meeting_url=join_url,
            calendar_event_id=calendar_event_id,
            calendar_html_link=calendar_html_link,
            invite_status="queued",
            updated_at=utcnow(),
        )
    )
    await session.commit()

    comm, action = await _queue_confirmation(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        candidate=candidate,
        interview_id=interview_id,
        provider=row["provider"],
        start=new_start,
        timezone_name=timezone_name,
        join_url=join_url,
        notes=row["notes"],
        kind="reschedule",
    )

    await log_activity(
        session,
        workspace_id=workspace_id,
        recruiter_id=recruiter_id,
        action="interview.rescheduled",
        entity_type="candidate",
        entity_id=row["candidate_id"],
        metadata={"interview_id": interview_id, "start": new_start.isoformat()},
    )
    await session.commit()
    return {
        "status": "rescheduled",
        "interview_id": interview_id,
        "start_time": new_start.isoformat(),
        "meeting_url": join_url,
        "confirmation_email": comm,
        "confirmation_action": action,
    }


async def execute_pending_action(
    session: AsyncSession, *, workspace_id: str, action_id: str, recruiter_id: str | None
) -> dict[str, Any]:
    row = (
        await session.execute(
            sa.select(pending_actions).where(
                pending_actions.c.id == action_id, pending_actions.c.workspace_id == workspace_id
            )
        )
    ).mappings().first()
    if not row:
        raise ValueError("Action not found")
    if row["status"] != "pending":
        return {"status": row["status"], "already_processed": True, "result": row["result"]}

    await session.execute(
        sa.update(pending_actions)
        .where(pending_actions.c.id == action_id)
        .values(status="approved")
    )
    await session.commit()

    try:
        if row["kind"] == "schedule_interview":
            result = await execute_schedule(
                session,
                workspace_id=workspace_id,
                recruiter_id=recruiter_id,
                payload=row["payload"] or {},
            )
        elif row["kind"] == "send_email":
            from .communications import send_communication

            result = await send_communication(
                session,
                workspace_id=workspace_id,
                communication_id=(row["payload"] or {}).get("communication_id"),
                recruiter_id=recruiter_id,
            )
        elif row["kind"] == "cancel_interview":
            result = await cancel_interview(
                session,
                workspace_id=workspace_id,
                interview_id=(row["payload"] or {}).get("interview_id"),
                recruiter_id=recruiter_id,
                reason=(row["payload"] or {}).get("reason"),
            )
        else:
            raise ValueError(f"Unsupported action: {row['kind']}")
    except (IntegrationError, ValueError) as exc:
        await session.execute(
            sa.update(pending_actions)
            .where(pending_actions.c.id == action_id)
            .values(status="failed", error_message=str(exc)[:500], executed_at=utcnow())
        )
        await session.commit()
        raise

    await session.execute(
        sa.update(pending_actions)
        .where(pending_actions.c.id == action_id)
        .values(status="executed", result=result, executed_at=utcnow())
    )
    await session.commit()
    return {"status": "executed", "result": result}


async def upcoming_interviews(session: AsyncSession, workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
    stmt = (
        sa.select(interviews, candidates.c.full_name.label("candidate_name"))
        .join(candidates, candidates.c.id == interviews.c.candidate_id)
        .where(interviews.c.workspace_id == workspace_id)
        .order_by(interviews.c.start_time.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]
