"""Meeting providers: real Microsoft Teams and Google Meet links.

Scheduling policy for this product:

* **Gmail** is the only email channel (ingestion + outbound invitations).
* The workspace chooses **Microsoft Teams** or **Google Meet** per meeting. A
  real online meeting is created through the documented Composio tools; its
  URL is stored and embedded in the Gmail invitation.
* Google Meet events are created on the recruiter's Google Calendar with
  ``create_meeting_room`` so the calendar event *and* the Meet link are real.
  If only the Google Meet toolkit is connected, a standalone Meet space is
  created (no calendar event) and that is recorded honestly on the row.
* Teams does not expose the recruiter's Outlook calendar through the toolkit
  used here, so conflict detection is based on interviews this product created
  (the local ``interviews`` table) and the configured working hours.

The join URL is never synthesised: if the provider does not return one, an
:class:`IntegrationError` is raised and the interview is not marked scheduled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..composio import IntegrationError, composio, normalize_tool_data
from ..config import settings

MEETING_PROVIDERS = ("microsoft_teams", "google_meet", "none")


@dataclass(slots=True)
class TimeSlot:
    start: datetime
    end: datetime

    def overlaps(self, other: "TimeSlot") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(slots=True)
class MeetingLink:
    provider: str
    meeting_id: str | None
    join_url: str | None
    created: bool
    raw: dict[str, Any] | None = None
    # Where the event lives, if the provider also created a calendar event.
    calendar_provider: str = "none"
    calendar_id: str | None = None
    calendar_event_id: str | None = None
    calendar_html_link: str | None = None


def _find_join_url(value: Any, depth: int = 0) -> str | None:
    """Depth-first search for the first Microsoft Teams join URL.

    Composio envelopes vary between toolkit versions (``joinUrl``,
    ``joinWebUrl``, nested under ``onlineMeeting`` or ``response_data``), so we
    search the payload rather than assuming one shape.
    """
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key in ("joinWebUrl", "joinUrl", "join_url", "joinWebURL"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
        for nested in value.values():
            found = _find_join_url(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_join_url(item, depth + 1)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                import json

                return _find_join_url(json.loads(text), depth + 1)
            except (ValueError, TypeError):
                return None
    return None


def _find_meeting_id(value: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key in ("id", "meetingId", "onlineMeetingId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate) > 8:
                return candidate
        for nested in value.values():
            found = _find_meeting_id(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_meeting_id(item, depth + 1)
            if found:
                return found
    elif isinstance(value, str) and (value.startswith("{") or value.startswith("[")):
        try:
            import json

            return _find_meeting_id(json.loads(value), depth + 1)
        except (ValueError, TypeError):
            return None
    return None


def _find_google_meet_url(value: Any, depth: int = 0) -> str | None:
    """Depth-first search for a Google Meet join URL.

    Google Calendar returns ``hangoutLink``; the Meet API returns
    ``meetingUri`` / ``meeting_uri``. Composio may wrap either in
    ``response_data``.
    """
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key in (
            "hangoutLink",
            "hangout_link",
            "meetingUri",
            "meeting_uri",
            "meetingUrl",
            "meeting_url",
            "videoConferenceUrl",
            "conferenceUrl",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
        for nested in value.values():
            found = _find_google_meet_url(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_google_meet_url(item, depth + 1)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                import json

                return _find_google_meet_url(json.loads(text), depth + 1)
            except (ValueError, TypeError):
                return None
    return None


def _find_html_link(value: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key in ("htmlLink", "html_link", "htmlUrl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
        for nested in value.values():
            found = _find_html_link(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_html_link(item, depth + 1)
            if found:
                return found
    return None


class TeamsMeetingProvider:
    """Creates/updates/deletes real Microsoft Teams online meetings."""

    name = "microsoft_teams"

    def __init__(self, account: dict[str, Any] | None) -> None:
        self.account = account or {}
        self.connected = bool(
            account
            and account.get("composio_connected_account_id")
            and account.get("status") in (None, "active")
        )
        self.user_id = self.account.get("composio_user_id") or ""
        self.connected_account_id = self.account.get("composio_connected_account_id")

    async def _execute(self, slug: str, arguments: dict[str, Any]) -> Any:
        if not self.connected:
            raise IntegrationError(
                "Microsoft Teams is not connected. Connect it from Settings → Integrations "
                "before scheduling interviews.",
                provider="microsoft_teams",
                code="not_connected",
            )
        return await composio.execute(
            slug,
            user_id=self.user_id,
            arguments=arguments,
            connected_account_id=self.connected_account_id,
        )

    async def create_meeting(
        self,
        *,
        subject: str,
        start: datetime,
        end: datetime,
        external_id: str | None = None,
    ) -> MeetingLink:
        """Create a standalone Teams meeting.

        Prefers ``MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING`` because it is
        idempotent on ``externalId``; falls back to the simpler documented
        ``MICROSOFT_TEAMS_CREATE_MEETING`` when that variant is unavailable on
        the connected toolkit version.
        """
        start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        payload: Any = None
        if external_id:
            payload = await self._execute(
                "MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING",
                {
                    "externalId": external_id[:256],
                    "subject": subject,
                    "startDateTime": start_iso,
                    "endDateTime": end_iso,
                },
            )
        else:
            payload = await self._execute(
                "MICROSOFT_TEAMS_CREATE_MEETING",
                {
                    "subject": subject,
                    "start_date_time": start_iso,
                    "end_date_time": end_iso,
                },
            )

        data = normalize_tool_data(payload)
        join_url = _find_join_url(data)
        if not join_url:
            # Retry with the alternate documented tool before giving up.
            alternate = (
                "MICROSOFT_TEAMS_CREATE_MEETING"
                if external_id
                else "MICROSOFT_TEAMS_CREATE_OR_GET_ONLINE_MEETING"
            )
            arguments: dict[str, Any] = (
                {
                    "subject": subject,
                    "start_date_time": start_iso,
                    "end_date_time": end_iso,
                }
                if alternate == "MICROSOFT_TEAMS_CREATE_MEETING"
                else {
                    "externalId": (external_id or subject)[:256],
                    "subject": subject,
                    "startDateTime": start_iso,
                    "endDateTime": end_iso,
                }
            )
            payload = await self._execute(alternate, arguments)
            data = normalize_tool_data(payload)
            join_url = _find_join_url(data)

        if not join_url:
            raise IntegrationError(
                "Microsoft Teams created a meeting without a join URL",
                provider="microsoft_teams",
                code="bad_response",
                details={"keys": sorted(data.keys())},
            )

        return MeetingLink(
            provider="microsoft_teams",
            meeting_id=_find_meeting_id(data),
            join_url=join_url,
            created=True,
            raw=data,
            calendar_provider="teams",
        )

    async def update_meeting(
        self,
        *,
        meeting_id: str,
        subject: str,
        start: datetime,
        end: datetime,
    ) -> MeetingLink:
        start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = await self._execute(
            "MICROSOFT_TEAMS_UPDATE_MEETING",
            {
                "online_meeting_id": meeting_id,
                "subject": subject,
                "start_date_time": start_iso,
                "end_date_time": end_iso,
            },
        )
        data = normalize_tool_data(payload)
        return MeetingLink(
            provider="microsoft_teams",
            meeting_id=meeting_id,
            join_url=_find_join_url(data),
            created=False,
            raw=data,
            calendar_provider="teams",
        )

    async def delete_meeting(self, meeting_id: str) -> None:
        await self._execute(
            "MICROSOFT_TEAMS_DELETE_USER_ONLINE_MEETING",
            {"user_id": "me", "online_meeting_id": meeting_id},
        )


class GoogleMeetProvider:
    """Creates real Google Meet links, and the Google Calendar event if connected.

    Preferred path: ``GOOGLECALENDAR_CREATE_EVENT`` with
    ``create_meeting_room=True`` returns the event plus its ``hangoutLink``.
    Google Workspace accounts always get a Meet link this way; personal Gmail
    accounts may not (Google silently omits the conference), in which case we
    fall back to a standalone ``GOOGLEMEET_CREATE_MEET`` space when the Meet
    toolkit is connected.

    ``send_updates="none"`` keeps Google from emailing attendees: the product's
    own approval-gated Gmail invitation is the single source of truth.
    """

    name = "google_meet"

    def __init__(
        self,
        calendar_account: dict[str, Any] | None,
        meet_account: dict[str, Any] | None,
    ) -> None:
        self.calendar_account = calendar_account or {}
        self.meet_account = meet_account or {}
        self.calendar_connected = self._active(self.calendar_account)
        self.meet_connected = self._active(self.meet_account)
        self.connected = self.calendar_connected or self.meet_connected

    @staticmethod
    def _active(account: dict[str, Any]) -> bool:
        return bool(
            account
            and account.get("composio_connected_account_id")
            and account.get("status") in (None, "active")
        )

    async def _execute(self, slug: str, arguments: dict[str, Any], account: dict[str, Any]) -> Any:
        if not self._active(account):
            raise IntegrationError(
                "Google is not connected. Connect Google Calendar or Google Meet from "
                "Settings → Integrations before scheduling interviews.",
                provider="google_meet",
                code="not_connected",
            )
        return await composio.execute(
            slug,
            user_id=account.get("composio_user_id") or "",
            arguments=arguments,
            connected_account_id=account.get("composio_connected_account_id"),
        )

    async def create_meeting(
        self,
        *,
        subject: str,
        start: datetime,
        end: datetime,
        external_id: str | None = None,
        timezone_name: str = "UTC",
        attendee_email: str | None = None,
        description: str | None = None,
    ) -> MeetingLink:
        if not self.connected:
            raise IntegrationError(
                "Google Meet is not connected. Connect Google Calendar or Google Meet from "
                "Settings → Integrations before scheduling interviews.",
                provider="google_meet",
                code="not_connected",
            )

        start_iso = start.astimezone(timezone.utc).isoformat()
        end_iso = end.astimezone(timezone.utc).isoformat()
        attendees = (
            [{"email": attendee_email}] if attendee_email else []
        )

        event_data: dict[str, Any] = {}
        event_id: str | None = None
        html_link: str | None = None
        join_url: str | None = None

        if self.calendar_connected:
            # Creating the calendar event also creates the Meet conference.
            event_data = normalize_tool_data(
                await self._execute(
                    "GOOGLECALENDAR_CREATE_EVENT",
                    {
                        "summary": subject,
                        "description": description or f"Interview: {subject}",
                        "start_datetime": start_iso,
                        "end_datetime": end_iso,
                        "timezone": timezone_name,
                        "calendar_id": "primary",
                        "attendees": attendees,
                        "create_meeting_room": True,
                        "send_updates": "none",
                    },
                    self.calendar_account,
                )
            )
            event_id = _find_meeting_id(event_data)
            html_link = _find_html_link(event_data)
            join_url = _find_google_meet_url(event_data)

        if not join_url and self.meet_connected:
            # Standalone space: no calendar event, but a real Meet link.
            meet_data = normalize_tool_data(
                await self._execute("GOOGLEMEET_CREATE_MEET", {}, self.meet_account)
            )
            join_url = _find_google_meet_url(meet_data)
            if event_id and join_url:
                # Attach the standalone Meet link to the calendar event so the
                # invite and the calendar agree on the same room.
                try:
                    patched = normalize_tool_data(
                        await self._execute(
                            "GOOGLECALENDAR_PATCH_EVENT",
                            {
                                "event_id": event_id,
                                "calendar_id": "primary",
                                "location": join_url,
                                "description": f"Join Google Meet: {join_url}",
                                "send_updates": "none",
                            },
                            self.calendar_account,
                        )
                    )
                    html_link = html_link or _find_html_link(patched)
                except IntegrationError:
                    # The event and link still exist; only the location field
                    # could not be updated, so keep going with the real link.
                    pass
            if not event_id:
                event_id = _find_meeting_id(meet_data)
            event_data = {**event_data, **meet_data}

        if not join_url:
            raise IntegrationError(
                "Google did not return a Meet link. Calendar events created on a personal "
                "Gmail account cannot attach a Meet room — connect Google Meet or use a "
                "Google Workspace account, or choose Microsoft Teams.",
                provider="google_meet",
                code="bad_response",
                details={"keys": sorted(event_data.keys())},
            )

        return MeetingLink(
            provider="google_meet",
            meeting_id=event_id,
            join_url=join_url,
            created=True,
            raw=event_data,
            calendar_provider="google_calendar" if self.calendar_connected and event_id else "google_meet",
            calendar_id="primary" if self.calendar_connected and event_id else None,
            calendar_event_id=event_id if self.calendar_connected else None,
            calendar_html_link=html_link,
        )

    async def update_meeting(
        self,
        *,
        meeting_id: str,
        subject: str,
        start: datetime,
        end: datetime,
        timezone_name: str = "UTC",
        join_url: str | None = None,
    ) -> MeetingLink:
        if not self.calendar_connected or not meeting_id:
            # A standalone Meet space is permanent, so there is nothing to
            # reschedule on Google's side; reuse the existing real link.
            return MeetingLink(
                provider="google_meet",
                meeting_id=meeting_id,
                join_url=join_url,
                created=False,
                calendar_provider="google_meet",
            )
        data = normalize_tool_data(
            await self._execute(
                "GOOGLECALENDAR_PATCH_EVENT",
                {
                    "event_id": meeting_id,
                    "calendar_id": "primary",
                    "summary": subject,
                    "start_time": start.astimezone(timezone.utc).isoformat(),
                    "end_time": end.astimezone(timezone.utc).isoformat(),
                    "timezone": timezone_name,
                    "send_updates": "none",
                },
                self.calendar_account,
            )
        )
        return MeetingLink(
            provider="google_meet",
            meeting_id=meeting_id,
            join_url=_find_google_meet_url(data) or join_url,
            created=False,
            raw=data,
            calendar_provider="google_calendar",
            calendar_id="primary",
            calendar_event_id=meeting_id,
            calendar_html_link=_find_html_link(data),
        )

    async def delete_meeting(self, meeting_id: str) -> None:
        if not self.calendar_connected or not meeting_id:
            return
        await self._execute(
            "GOOGLECALENDAR_DELETE_EVENT",
            {"event_id": meeting_id, "calendar_id": "primary", "send_updates": "none"},
            self.calendar_account,
        )


def _ensure_aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def working_window(day: datetime, *, timezone_name: str) -> TimeSlot:
    tz = ZoneInfo(timezone_name)
    local = day.astimezone(tz)
    start = datetime.combine(local.date(), time(settings.working_hours_start, 0), tzinfo=tz)
    end = datetime.combine(local.date(), time(settings.working_hours_end, 0), tzinfo=tz)
    return TimeSlot(start.astimezone(timezone.utc), end.astimezone(timezone.utc))


def generate_alternatives(
    *,
    after: datetime,
    duration_minutes: int,
    busy: list[TimeSlot],
    existing: list[TimeSlot],
    timezone_name: str,
    days: int = 5,
    limit: int = 4,
) -> list[TimeSlot]:
    """Deterministic free-slot search inside working hours."""
    tz = ZoneInfo(timezone_name)
    blocked = busy + existing
    proposals: list[TimeSlot] = []
    now = datetime.now(timezone.utc)
    for day_offset in range(days):
        window = working_window(after + timedelta(days=day_offset), timezone_name=timezone_name)
        cursor = window.start
        if cursor < now.astimezone(timezone.utc):
            current_local = now.astimezone(tz)
            if current_local.date() == window.start.astimezone(tz).date():
                cursor = (current_local + timedelta(minutes=15)).replace(
                    minute=((current_local.minute // 15) + 1) % 60,
                    second=0,
                    microsecond=0,
                )
                if cursor.minute == 0:
                    cursor += timedelta(hours=1)
                cursor = cursor.astimezone(timezone.utc)
                cursor = max(cursor, window.start)
        while cursor + timedelta(minutes=duration_minutes) <= window.end:
            candidate = TimeSlot(cursor, cursor + timedelta(minutes=duration_minutes))
            if not any(candidate.overlaps(slot) for slot in blocked):
                proposals.append(candidate)
                if len(proposals) >= limit:
                    return proposals
            cursor += timedelta(minutes=30)
    return proposals


def local_label(slot: TimeSlot, timezone_name: str) -> str:
    tz = ZoneInfo(timezone_name)
    start = slot.start.astimezone(tz)
    end = slot.end.astimezone(tz)
    return f"{start.strftime('%a %d %b, %I:%M %p')} – {end.strftime('%I:%M %p')} ({timezone_name})"
