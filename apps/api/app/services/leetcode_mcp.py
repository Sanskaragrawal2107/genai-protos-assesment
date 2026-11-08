"""LeetCode evidence through the hosted MCP server.

This is an *optional* evidence source. The public GraphQL endpoint in
``verification.py`` is primary; this module is only consulted when
``LEETCODE_MCP_FALLBACK`` is enabled and GraphQL is unavailable.

The MCP server is an external, unfamiliar surface: tool names are discovered
per process when the server can list them, otherwise the documented tool names
are used. Any failure is returned as ``available: false`` with a reason —
never as zero activity — so an outage is never turned into a negative claim
about the candidate.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from ..config import settings

log = logging.getLogger("recruiter.leetcode_mcp")

# Documented tool names exposed by leetcode-mcp-server; used when the server
# cannot answer tools/list (it currently returns a JSON-RPC -32603 error).
_DEFAULT_TOOLS = {
    "profile": ("get_user_profile",),
    "contest": ("get_user_contest_ranking",),
}
_TOOL_HINTS = {
    "profile": ("user_profile", "profile"),
    "contest": ("contest_ranking", "contest"),
}
_tools: dict[str, str] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unavailable(username: str | None, reason: str) -> dict[str, Any]:
    return {
        "provider": "leetcode",
        "source": "leetcode-mcp",
        "source_url": f"https://leetcode.com/{username}/" if username else None,
        "username": username,
        "available": False,
        "reason": reason,
        "fetched_at": _now(),
    }


async def _resolve_tools(session: ClientSession) -> dict[str, str]:
    """Map logical capabilities to tool names, preferring live discovery."""
    global _tools
    if _tools is not None:
        return _tools

    discovered: set[str] = set()
    try:
        listing = await asyncio.wait_for(session.list_tools(), timeout=10)
        discovered = {tool.name for tool in listing.tools}
    except Exception as exc:  # noqa: BLE001 - discovery is best effort
        log.info("leetcode MCP tools/list unavailable (%s); using documented names", type(exc).__name__)

    resolved: dict[str, str] = {}
    for capability, defaults in _DEFAULT_TOOLS.items():
        match = next(
            (
                name
                for name in discovered
                if any(hint in name.lower() for hint in _TOOL_HINTS[capability])
            ),
            None,
        )
        resolved[capability] = match or defaults[0]
    _tools = resolved
    return resolved


def _search(document: Any, key: str):
    if isinstance(document, dict):
        if key in document:
            yield document[key]
        for value in document.values():
            yield from _search(value, key)
    elif isinstance(document, list):
        for value in document:
            yield from _search(value, key)


def _first(document: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        for value in _search(document, key):
            if value not in (None, "", [], {}):
                return value
    return default


def _difficulty_counts(entries: Any) -> dict[str, int]:
    if not isinstance(entries, list):
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        difficulty = entry.get("difficulty")
        count = entry.get("count")
        if isinstance(difficulty, str) and isinstance(count, int):
            counts[difficulty.lower()] = count
    return counts


def _result_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured:
        return structured
    texts = [
        getattr(block, "text", "")
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "text", "")
    ]
    if not texts:
        return None
    raw = "\n".join(texts).strip()
    try:
        return json.loads(raw)
    except ValueError:
        return {"text": raw}


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    result = await asyncio.wait_for(
        session.call_tool(name, arguments, read_timeout_seconds=None), timeout=18
    )
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise RuntimeError(f"{name} returned an error")
    payload = _result_payload(result)
    if payload is None:
        raise RuntimeError(f"{name} returned no content")
    return payload


def _normalise(username: str, profile_raw: Any, contest_raw: Any) -> dict[str, Any]:
    profile = profile_raw.get("profile", profile_raw) if isinstance(profile_raw, dict) else profile_raw
    ranking = _first(profile, "ranking")
    real_name = _first(profile, "realName", "real_name", "name")

    solved_counts = _difficulty_counts(_first(profile, "acSubmissionNum", "ac_submission_num"))
    submission_counts = _difficulty_counts(
        _first(profile, "totalSubmissionNum", "total_submission_num")
    )
    total_solved = _first(profile, "totalSolved", "total_solved") or solved_counts.get("all")
    total_submissions = _first(profile, "totalSubmissions", "total_submissions") or (
        submission_counts.get("all")
    )

    contest = _first(contest_raw, "userContestRanking", "user_contest_ranking", default={}) or {}
    rating = _first(contest, "rating")
    top_percentage = _first(contest, "topPercentage", "top_percentage")
    attended = _first(contest, "attendedContestsCount", "attended_contests_count")
    global_ranking = _first(contest, "globalRanking", "global_ranking")

    return {
        "provider": "leetcode",
        "source": "leetcode-mcp",
        "source_url": f"https://leetcode.com/{username}/",
        "username": username,
        "available": True,
        "reason": None,
        "profile_url": f"https://leetcode.com/{username}/",
        "real_name": real_name,
        "ranking": ranking,
        "total_solved": total_solved,
        "solved_by_difficulty": {
            key: value for key, value in solved_counts.items() if key != "all"
        },
        "total_submissions": total_submissions,
        "submissions_by_difficulty": {
            key: value for key, value in submission_counts.items() if key != "all"
        },
        "contest_rating": rating,
        "contest_global_ranking": global_ranking,
        "contest_top_percentage": top_percentage,
        "contests_attended": attended,
        "caveat": (
            "Public LeetCode profile and contest data show practice volume and "
            "competition rating, not verified engineering ability."
        ),
        "fetched_at": _now(),
    }


async def _fetch(username: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {settings.leetcode_mcp_token}"}
    # mcp 2.x vendors its own httpx (`httpx2`) and create_mcp_http_client expects
    # an httpx2.Timeout — passing an httpx.Timeout breaks its internal timeout
    # arithmetic. Rely on MCP's default timeouts; the whole fetch is bounded by
    # leetcode_mcp_timeout_seconds via asyncio.wait_for in fetch_leetcode_evidence.
    http_client = create_mcp_http_client(headers=headers)
    async with http_client:
        async with streamable_http_client(settings.leetcode_mcp_url, http_client=http_client) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=15)
                tools = await _resolve_tools(session)
                profile = await _call(
                    session, tools["profile"], {"username": username}
                )
                contest = None
                try:
                    contest = await _call(
                        session, tools["contest"], {"username": username}
                    )
                except Exception as exc:  # noqa: BLE001 - contest data is optional
                    log.info("leetcode MCP contest lookup failed: %s", type(exc).__name__)
                return _normalise(username, profile, contest)


async def fetch_leetcode_evidence(username: str) -> dict[str, Any]:
    """Profile + contest evidence for a public LeetCode username."""
    if not settings.leetcode_mcp_enabled:
        return _unavailable(username, "LeetCode MCP is not configured")
    try:
        return await asyncio.wait_for(
            _fetch(username), timeout=settings.leetcode_mcp_timeout_seconds
        )
    except asyncio.TimeoutError:
        return _unavailable(username, "LeetCode MCP timed out")
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        log.warning("leetcode MCP lookup failed for %s: %s", username, type(exc).__name__)
        return _unavailable(username, f"LeetCode MCP unavailable ({type(exc).__name__})")
