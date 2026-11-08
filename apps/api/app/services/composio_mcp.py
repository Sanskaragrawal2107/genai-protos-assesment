"""Composio hosted-MCP sessions used by the chatbot.

The recruiter assistant (``apps/mcp``) talks to Composio through a *scoped
session* rather than a global MCP server: each workspace gets its own
``user_id`` and only the toolkits it actually needs (Gmail + Teams + Google).
That keeps a workspace from reaching another workspace's connected accounts.

Credentials never leave the server. :func:`public_view` deliberately omits the
MCP auth headers (they contain the Composio API key) and the raw endpoint.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from ..composio import McpSession, composio
from ..config import settings

# The only toolkits this product needs. Inbound resumes and outbound candidate
# email go through Gmail; meetings are created on Microsoft Teams or Google.
TOOLKITS = ("gmail", "microsoft_teams", "googlecalendar", "googlemeet")


def composio_user_id(workspace_id: str) -> str:
    return f"{settings.composio_user_prefix}:{workspace_id}"


async def session_for_workspace(workspace_id: str) -> McpSession:
    """Create/reuse the workspace's hosted MCP session.

    Raises :class:`~app.composio.IntegrationError` when Composio is not
    configured, so callers can fall back to the deterministic tools instead of
    pretending the MCP bridge is available.
    """
    return await composio.create_mcp_session(
        user_id=composio_user_id(workspace_id),
        toolkits=list(TOOLKITS),
    )


def public_view(session: McpSession) -> dict[str, Any]:
    """Browser-safe description of a session (no secrets, no raw URL)."""
    host = urlparse(session.url).hostname or "unknown"
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "toolkits": session.toolkits,
        "endpoint_host": host,
        "has_credentials": bool(session.headers),
        "expires_in_seconds": max(0, int(25 * 60 - (time.time() - session.created_at))),
    }
