"""Deterministic natural-language date/time parsing for the assistant.

The conversational assistant itself lives in ``app/graphs/assistant_graph.py``
(an OpenAI tool-calling loop). This module keeps only the intent-free helper it
shares: :func:`parse_when`, which converts phrases like *"tomorrow at 3pm"* or
*"12th of November"* into an aware UTC ``datetime`` without any LLM call.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..db import utcnow

_MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _resolve_year_month_day(base: datetime, month: int, day: int, lowered: str) -> datetime | None:
    year = base.year
    y_match = re.search(r"\b(202\d)\b", lowered)
    if y_match:
        year = int(y_match.group(1))
    else:
        try:
            if datetime(year, month, day).date() < base.date():
                year += 1
        except ValueError:
            pass
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def parse_when(
    text: str | None, *, timezone_name: str = "Asia/Kolkata", now: datetime | None = None
) -> datetime | None:
    """Deterministic natural-language date/time parsing (no LLM)."""
    if not text:
        return None
    tz = ZoneInfo(timezone_name)
    base = (now or utcnow()).astimezone(tz)
    lowered = text.lower()

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lowered)
    if not time_match:
        time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", lowered)
    hour = minute = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        meridiem = time_match.group(3) if time_match.lastindex and time_match.lastindex >= 3 else None
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = 10, 0

    target_date = None

    # Explicit month and day: "12th of November", "12 Nov", "November 12th", "Nov 12".
    m_day_month = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+([a-zA-Z]+)\b", lowered)
    m_month_day = re.search(r"\b([a-zA-Z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", lowered)

    if m_day_month and m_day_month.group(2) in _MONTH_NAMES:
        target_date = _resolve_year_month_day(base, _MONTH_NAMES[m_day_month.group(2)], int(m_day_month.group(1)), lowered)
    elif m_month_day and m_month_day.group(1) in _MONTH_NAMES:
        target_date = _resolve_year_month_day(base, _MONTH_NAMES[m_month_day.group(1)], int(m_month_day.group(2)), lowered)
    elif "day after tomorrow" in lowered:
        target_date = base.date() + timedelta(days=2)
    elif "tomorrow" in lowered:
        target_date = base.date() + timedelta(days=1)
    elif "today" in lowered or "tonight" in lowered:
        target_date = base.date()
    else:
        for name, index in _WEEKDAYS.items():
            if name in lowered:
                delta = (index - base.weekday()) % 7 or 7
                target_date = base.date() + timedelta(days=delta)
                break

    if target_date is None:
        rel = re.search(r"in\s+(\d+)\s+(hour|hours|day|days)", lowered)
        if rel:
            amount = int(rel.group(1))
            if "hour" in rel.group(2):
                return (base + timedelta(hours=amount)).replace(second=0, microsecond=0).astimezone(timezone.utc)
            return (base + timedelta(days=amount)).replace(
                hour=hour or 10, minute=minute or 0, second=0, microsecond=0
            ).astimezone(timezone.utc)
        if not time_match:
            return None
        target_date = base.date() if (hour or 0) > base.hour else base.date() + timedelta(days=1)

    local = datetime.combine(target_date, datetime.min.time(), tzinfo=tz).replace(
        hour=hour or 10, minute=minute or 0
    )
    return local.astimezone(timezone.utc)
