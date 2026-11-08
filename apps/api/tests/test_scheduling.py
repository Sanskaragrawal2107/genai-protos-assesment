"""Availability, conflict detection and scheduling idempotency."""
from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
import pytest

from app import db as db_module
from app.composio import IntegrationError, normalize_tool_data
from app.db import candidate_communications, candidates, connected_accounts, interviews, pending_actions, utcnow
from app.services.calendar import _find_join_url
from app.services.interviews import _idempotency_key, availability, execute_schedule


async def _candidate(db, workspace_id: str) -> dict:
    cid = db_module.new_id()
    now = utcnow()
    await db.execute(
        sa.insert(candidates).values(
            id=cid,
            workspace_id=workspace_id,
            full_name="Interview Candidate",
            email="candidate@example.com",
            email_normalized="candidate@example.com",
            skills=[],
            previous_companies=[],
            education=[],
            projects=[],
            pipeline_status="SHORTLISTED",
            source="test",
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    return {"id": cid}


def test_idempotency_key_is_stable():
    start = utcnow().replace(microsecond=0)
    a = _idempotency_key("ws", "cand", start, "rec")
    b = _idempotency_key("ws", "cand", start, "rec")
    c = _idempotency_key("ws", "cand", start + timedelta(minutes=30), "rec")
    assert a == b
    assert a != c


def test_normalize_meeting_provider_aliases():
    from app.repo import normalize_meeting_provider

    assert normalize_meeting_provider("google_meet") == "google_meet"
    assert normalize_meeting_provider("Google Meet") == "google_meet"
    assert normalize_meeting_provider("google meet") == "google_meet"
    assert normalize_meeting_provider("meet") == "google_meet"
    assert normalize_meeting_provider("teams") == "microsoft_teams"
    assert normalize_meeting_provider(None) == "microsoft_teams"


def test_provider_hint_from_message():
    from app.graphs.assistant_graph import provider_hint_from_message

    assert provider_hint_from_message("go for Google Meet") == "google_meet"
    assert provider_hint_from_message("Okay switch to Google Meet.") == "google_meet"
    assert provider_hint_from_message("switch to gmeet please") == "google_meet"
    assert provider_hint_from_message("use Microsoft Teams instead") == "microsoft_teams"
    assert provider_hint_from_message("schedule it for 3pm tomorrow") is None
    assert provider_hint_from_message(None) is None
    assert provider_hint_from_message("") is None


async def test_availability_detects_local_conflicts(db, workspace):
    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    now = utcnow()
    await db.execute(
        sa.insert(interviews).values(
            id=db_module.new_id(),
            workspace_id=workspace["workspace_id"],
            candidate_id=candidate["id"],
            recruiter_id="tester",
            provider="google_meet",
            calendar_provider="google_calendar",
            calendar_id="primary",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="Asia/Kolkata",
            status="scheduled",
            invite_status="sent",
            attendees=[],
            idempotency_key="existing-interview",
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    check = await availability(
        db,
        workspace_id=workspace["workspace_id"],
        start=start + timedelta(minutes=10),
        duration_minutes=30,
        timezone_name="Asia/Kolkata",
    )
    assert check["available"] is False
    assert check["conflicts"]
    assert check["alternatives"]
    assert check["calendar_connected"] is False


async def test_availability_proposes_slots_when_free(db, workspace):
    start = (utcnow() + timedelta(days=2)).replace(hour=4, minute=0, second=0, microsecond=0)
    check = await availability(
        db,
        workspace_id=workspace["workspace_id"],
        start=start,
        duration_minutes=30,
        timezone_name="Asia/Kolkata",
    )
    assert check["available"] is True
    assert check["conflicts"] == []


async def test_schedule_never_fakes_a_meeting_without_calendar(db, workspace):
    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    payload = {
        "candidate_id": candidate["id"],
        "start": start.isoformat(),
        "duration_minutes": 30,
        "timezone": "Asia/Kolkata",
        "provider": "google_meet",
        "idempotency_key": _idempotency_key(workspace["workspace_id"], candidate["id"], start, "tester"),
    }
    with pytest.raises(IntegrationError) as exc:
        await execute_schedule(
            db, workspace_id=workspace["workspace_id"], recruiter_id="tester", payload=payload
        )
    assert exc.value.code == "not_connected"

    rows = (await db.execute(sa.select(interviews))).mappings().all()
    assert rows == []


async def test_duplicate_schedule_attempt_returns_existing(db, workspace):
    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=4)).replace(minute=0, second=0, microsecond=0)
    key = _idempotency_key(workspace["workspace_id"], candidate["id"], start, "tester")
    now = utcnow()
    await db.execute(
        sa.insert(interviews).values(
            id=db_module.new_id(),
            workspace_id=workspace["workspace_id"],
            candidate_id=candidate["id"],
            recruiter_id="tester",
            provider="google_meet",
            calendar_provider="google_calendar",
            calendar_id="primary",
            start_time=start,
            end_time=start + timedelta(minutes=30),
            timezone="Asia/Kolkata",
            status="scheduled",
            invite_status="sent",
            attendees=[],
            idempotency_key=key,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    result = await execute_schedule(
        db,
        workspace_id=workspace["workspace_id"],
        recruiter_id="tester",
        payload={
            "candidate_id": candidate["id"],
            "start": start.isoformat(),
            "duration_minutes": 30,
            "timezone": "Asia/Kolkata",
            "provider": "google_meet",
            "idempotency_key": key,
        },
    )
    assert result["status"] == "already_scheduled"
    assert len((await db.execute(sa.select(interviews))).mappings().all()) == 1


def test_join_url_extraction_handles_composio_envelopes():
    assert (
        _find_join_url(
            normalize_tool_data(
                {"response_data": {"onlineMeeting": {"joinWebUrl": "https://teams.microsoft.com/l/meetup-join/x"}}}
            )
        )
        == "https://teams.microsoft.com/l/meetup-join/x"
    )
    assert _find_join_url({"data": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/y"}}) is not None
    assert _find_join_url({"subject": "no link here"}) is None


async def _connect_teams(db, workspace_id: str) -> None:
    now = utcnow()
    await db.execute(
        sa.insert(connected_accounts).values(
            id=db_module.new_id(),
            workspace_id=workspace_id,
            recruiter_id="tester",
            provider="microsoft_teams",
            composio_connected_account_id="ca_teams",
            composio_user_id="airecruiter:" + workspace_id,
            connected_email="recruiting@example.com",
            status="active",
            scopes=[],
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()


async def test_schedule_creates_real_teams_meeting_and_queues_gmail_invite(db, workspace, monkeypatch):
    """The real Teams join URL lands on the row; no Gmail send happens yet."""
    await _connect_teams(db, workspace["workspace_id"])
    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=5)).replace(minute=0, second=0, microsecond=0)

    calls: list[tuple[str, dict]] = []

    async def fake_execute(slug: str, *, user_id: str, arguments: dict, connected_account_id=None):
        calls.append((slug, arguments))
        return {
            "id": "meeting-123",
            "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/abc123",
            "subject": arguments.get("subject"),
        }

    monkeypatch.setattr("app.services.calendar.composio.execute", fake_execute)

    result = await execute_schedule(
        db,
        workspace_id=workspace["workspace_id"],
        recruiter_id="tester",
        payload={
            "candidate_id": candidate["id"],
            "start": start.isoformat(),
            "duration_minutes": 30,
            "timezone": "Asia/Kolkata",
            "provider": "microsoft_teams",
            "idempotency_key": _idempotency_key(workspace["workspace_id"], candidate["id"], start, "tester"),
        },
    )

    assert result["status"] == "scheduled"
    assert result["provider"] == "microsoft_teams"
    assert result["meeting_url"] == "https://teams.microsoft.com/l/meetup-join/abc123"
    assert result["meeting_id"] == "meeting-123"
    assert calls and calls[0][0] in {
        "MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING",
        "MICROSOFT_TEAMS_CREATE_MEETING",
    }

    interview = (await db.execute(sa.select(interviews))).mappings().one()
    assert interview["provider"] == "microsoft_teams"
    assert interview["calendar_provider"] == "teams"
    assert interview["meeting_url"] == "https://teams.microsoft.com/l/meetup-join/abc123"
    assert interview["status"] == "scheduled"

    # No Gmail send happens during scheduling: only a queued confirmation draft
    # plus an approval-gated send action.
    comms = (await db.execute(sa.select(candidate_communications))).mappings().all()
    assert len(comms) == 1
    assert comms[0]["provider"] == "gmail"
    assert "teams.microsoft.com" in (comms[0]["content"] or "")
    actions = (await db.execute(sa.select(pending_actions))).mappings().all()
    assert [a["kind"] for a in actions] == ["send_email"]


async def test_schedule_creates_google_calendar_event_with_meet_link(db, workspace, monkeypatch):
    now = utcnow()
    await db.execute(
        sa.insert(connected_accounts).values(
            id=db_module.new_id(),
            workspace_id=workspace["workspace_id"],
            recruiter_id="tester",
            provider="google_calendar",
            composio_connected_account_id="ca_google",
            composio_user_id="airecruiter:" + workspace["workspace_id"],
            connected_email="recruiting@example.com",
            status="active",
            scopes=[],
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)

    calls: list[tuple[str, dict]] = []

    async def fake_execute(slug: str, *, user_id: str, arguments: dict, connected_account_id=None):
        calls.append((slug, arguments))
        return {
            "id": "evt-google-1",
            "htmlLink": "https://calendar.google.com/event?eid=evt-google-1",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "summary": arguments.get("summary"),
        }

    monkeypatch.setattr("app.services.calendar.composio.execute", fake_execute)

    result = await execute_schedule(
        db,
        workspace_id=workspace["workspace_id"],
        recruiter_id="tester",
        payload={
            "candidate_id": candidate["id"],
            "start": start.isoformat(),
            "duration_minutes": 30,
            "timezone": "Asia/Kolkata",
            "provider": "google_meet",
            "idempotency_key": _idempotency_key(workspace["workspace_id"], candidate["id"], start, "tester"),
        },
    )

    assert result["status"] == "scheduled"
    assert result["provider"] == "google_meet"
    assert result["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert result["calendar_provider"] == "google_calendar"
    assert result["calendar_event_id"] == "evt-google-1"
    assert calls and calls[0][0] == "GOOGLECALENDAR_CREATE_EVENT"
    assert calls[0][1]["create_meeting_room"] is True
    assert calls[0][1]["send_updates"] == "none"
    assert calls[0][1]["attendees"] == [{"email": "candidate@example.com"}]

    interview = (await db.execute(sa.select(interviews))).mappings().one()
    assert interview["provider"] == "google_meet"
    assert interview["calendar_provider"] == "google_calendar"
    assert interview["calendar_event_id"] == "evt-google-1"
    assert interview["meeting_url"] == "https://meet.google.com/abc-defg-hij"

    comms = (await db.execute(sa.select(candidate_communications))).mappings().all()
    assert len(comms) == 1
    assert "meet.google.com" in (comms[0]["content"] or "")


async def test_schedule_is_idempotent_on_teams_external_id(db, workspace, monkeypatch):
    await _connect_teams(db, workspace["workspace_id"])
    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=6)).replace(minute=0, second=0, microsecond=0)
    seen: list[dict] = []

    async def fake_execute(slug: str, *, user_id: str, arguments: dict, connected_account_id=None):
        seen.append(arguments)
        return {"id": "meeting-xyz", "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/xyz"}

    monkeypatch.setattr("app.services.calendar.composio.execute", fake_execute)
    payload = {
        "candidate_id": candidate["id"],
        "start": start.isoformat(),
        "duration_minutes": 30,
        "timezone": "Asia/Kolkata",
        "provider": "microsoft_teams",
        "idempotency_key": _idempotency_key(workspace["workspace_id"], candidate["id"], start, "tester"),
    }

    first = await execute_schedule(db, workspace_id=workspace["workspace_id"], recruiter_id="tester", payload=payload)
    second = await execute_schedule(db, workspace_id=workspace["workspace_id"], recruiter_id="tester", payload=payload)

    assert first["status"] == "scheduled"
    assert second["status"] == "already_scheduled"
    assert len(seen) == 1  # no duplicate Teams meeting
    assert len((await db.execute(sa.select(interviews))).mappings().all()) == 1


async def test_provider_switch_rebuilds_meeting_in_place(db, workspace, monkeypatch):
    """Switching providers for a slot that is already booked must replace the
    meeting link instead of returning the stale one off the idempotency key."""
    await _connect_teams(db, workspace["workspace_id"])
    now = utcnow()
    await db.execute(
        sa.insert(connected_accounts).values(
            id=db_module.new_id(),
            workspace_id=workspace["workspace_id"],
            recruiter_id="tester",
            provider="google_calendar",
            composio_connected_account_id="ca_google",
            composio_user_id="airecruiter:" + workspace["workspace_id"],
            connected_email="recruiting@example.com",
            status="active",
            scopes=[],
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()

    candidate = await _candidate(db, workspace["workspace_id"])
    start = (utcnow() + timedelta(days=5)).replace(minute=0, second=0, microsecond=0)
    key = _idempotency_key(workspace["workspace_id"], candidate["id"], start, "tester")

    async def fake_execute(slug: str, *, user_id: str, arguments: dict, connected_account_id=None):
        if slug == "GOOGLECALENDAR_CREATE_EVENT":
            return {
                "id": "evt-google-1",
                "htmlLink": "https://calendar.google.com/event?eid=evt-google-1",
                "hangoutLink": "https://meet.google.com/abc-defg-hij",
                "summary": arguments.get("summary"),
            }
        return {"id": "meeting-xyz", "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/xyz"}

    monkeypatch.setattr("app.services.calendar.composio.execute", fake_execute)

    base = {
        "candidate_id": candidate["id"],
        "start": start.isoformat(),
        "duration_minutes": 30,
        "timezone": "Asia/Kolkata",
        "idempotency_key": key,
    }

    first = await execute_schedule(
        db,
        workspace_id=workspace["workspace_id"],
        recruiter_id="tester",
        payload={**base, "provider": "microsoft_teams"},
    )
    assert first["status"] == "scheduled"
    assert first["provider"] == "microsoft_teams"
    assert "teams.microsoft.com" in first["meeting_url"]

    switched = await execute_schedule(
        db,
        workspace_id=workspace["workspace_id"],
        recruiter_id="tester",
        payload={**base, "provider": "google_meet"},
    )
    assert switched["status"] == "scheduled"
    assert switched["provider"] == "google_meet"
    assert switched["meeting_url"] == "https://meet.google.com/abc-defg-hij"

    rows = (await db.execute(sa.select(interviews))).mappings().all()
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0]["id"] == switched["interview_id"] == first["interview_id"]
    assert rows[0]["provider"] == "google_meet"
    assert rows[0]["meeting_url"] == "https://meet.google.com/abc-defg-hij"
