"""Shared outbound HTTP client.

Every external call (OpenAI-compatible LLM, Composio, Supabase Storage, GitHub,
LeetCode) reuses one pooled :class:`httpx.AsyncClient` instead of constructing a
fresh client — and a fresh TLS handshake — per request. Per-request timeouts and
headers are still passed at the call site, so behaviour is unchanged.

An ``httpx.AsyncClient`` is bound to the event loop that created it, so the
client is recreated if the running loop changes (this matters for tests and for
workers that run more than one loop). The client is created lazily — after
``config`` has sanitised ``NO_PROXY`` — and closed by the FastAPI lifespan.
"""
from __future__ import annotations

import asyncio

import httpx

_client: httpx.AsyncClient | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_client() -> httpx.AsyncClient:
    global _client, _loop
    loop = _running_loop()
    if _client is None or _client.is_closed or (loop is not None and _loop is not loop):
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
        _loop = loop
    return _client


async def close_client() -> None:
    global _client, _loop
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    _loop = None
