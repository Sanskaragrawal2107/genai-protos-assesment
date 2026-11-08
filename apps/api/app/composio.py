"""Composio integration client (official SDK).

This module is the **only** place in the backend that talks to Composio. It is a
thin async adapter over the official ``composio`` Python SDK (>= 0.31) so the
rest of the codebase never deals with SDK types or blocking calls:

* ``tools.execute``                     – run a Gmail / Microsoft Teams tool
* ``auth_configs.list`` / ``create``    – resolve (or bootstrap) a toolkit auth config
* ``connected_accounts.link``           – hosted OAuth link for a toolkit
* ``connected_accounts.list``           – connection status
* ``connected_accounts.delete``         – revoke a connection on disconnect
* ``sessions.create(..., mcp=True)``    – scoped hosted MCP endpoint for the chatbot

The SDK is synchronous, so every call is dispatched with
:func:`asyncio.to_thread`. Failures are surfaced as :class:`IntegrationError`
instead of silent no-ops. Nothing here fabricates a result: if a toolkit is not
connected or the call fails, the caller receives an error it must handle.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import settings

log = logging.getLogger("recruiter.composio")


class IntegrationError(RuntimeError):
    """An external integration could not complete the requested action."""

    def __init__(self, message: str, *, provider: str, code: str = "integration_error", details: Any = None):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.details = details


def _short(value: Any, limit: int = 400) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalise SDK pydantic models / TypedDicts to plain JSON-ready dicts."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True)
    return {}


@dataclass(slots=True)
class McpSession:
    """A scoped Composio hosted-MCP session for one workspace/user.

    ``headers`` contains the Composio API key and must never reach a browser.
    It is only used server-side (the FastMCP bridge in ``apps/mcp``).
    """

    session_id: str
    user_id: str
    toolkits: list[str]
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class ComposioClient:
    def __init__(self) -> None:
        self._sdk: Any | None = None
        self._mcp_sessions: dict[str, McpSession] = {}
        self._tool_versions: dict[str, str] = {}
        self._toolkit_versions: dict[str, str] = {}
        configured = (settings.composio_toolkit_version or "").strip()
        # Manual execution rejects "latest", so only a concrete pin is used here;
        # otherwise the toolkit's current version is resolved at call time.
        self.version = configured if configured and configured.lower() != "latest" else None

    @property
    def api_key(self) -> str:
        return settings.composio_api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())

    def _client(self) -> Any:
        if not self.enabled:
            raise IntegrationError(
                "COMPOSIO_API_KEY is not configured", provider="composio", code="not_configured"
            )
        if self._sdk is None:
            try:
                from composio import Composio  # imported lazily so the SDK is loaded on demand
            except ImportError as exc:  # pragma: no cover - dependency error
                raise IntegrationError(
                    "The composio package is not installed. Run `pip install composio`.",
                    provider="composio",
                    code="not_installed",
                ) from exc
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if settings.composio_base_url:
                kwargs["base_url"] = settings.composio_base_url.rstrip("/")
            self._sdk = Composio(**kwargs)
        return self._sdk

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking SDK call in a worker thread and map its errors."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except IntegrationError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK exception hierarchy varies by version
            raise self._map_error(exc) from exc

    def _map_error(self, exc: Exception) -> IntegrationError:
        name = exc.__class__.__name__
        code = "provider_error"
        if name in {"ApiKeyError", "ApiKeyNotProvidedError", "UserApiKeyNotProvidedError"}:
            code = "not_configured"
        elif name == "ComposioMultipleConnectedAccountsError":
            code = "already_connected"
        elif name in {"ComposioSDKTimeoutError", "APITimeoutError", "TimeoutException"}:
            code = "timeout"
        elif name in {"NotFoundError", "ConnectedAccountNotFoundError", "ToolNotFoundError"}:
            code = "not_found"
        elif name in {"ConnectionError", "APIConnectionError"}:
            code = "transport"
        log.warning("composio error %s: %s", name, _short(exc))
        return IntegrationError(_short(exc) or name, provider="composio", code=code, details={"type": name})

    # ------------------------------------------------------------------ tools
    def _resolve_version(self, client: Any, tool_slug: str) -> str:
        """Return a concrete toolkit version for manual tool execution.

        The SDK raises ``ToolVersionRequiredError`` when a manual
        ``tools.execute`` call resolves to "latest". Versions are therefore
        read from the project's toolkit metadata and cached per process, so no
        toolkit version has to be hardcoded or configured.
        """
        cached = self._tool_versions.get(tool_slug)
        if cached:
            return cached

        tool = client.tools.get_raw_composio_tool_by_slug(tool_slug)
        toolkit = getattr(tool, "toolkit", None)
        toolkit_slug = getattr(toolkit, "slug", None) if toolkit is not None else None

        version = self._toolkit_versions.get(toolkit_slug or "")
        if not version and toolkit_slug:
            meta = getattr(client.toolkits.get(toolkit_slug), "meta", None)
            version = getattr(meta, "version", None)
            if version:
                self._toolkit_versions[toolkit_slug] = version
        if not version:
            version = getattr(tool, "version", None)
        if not version:
            raise IntegrationError(
                f"Composio did not return a toolkit version for {tool_slug}",
                provider="composio",
                code="bad_response",
            )

        self._tool_versions[tool_slug] = version
        log.info("composio toolkit %s resolved to version %s", toolkit_slug or tool_slug, version)
        return version

    async def execute(
        self,
        tool_slug: str,
        *,
        user_id: str,
        arguments: dict[str, Any],
        connected_account_id: str | None = None,
    ) -> Any:
        """Execute a toolkit tool and return its ``data`` payload."""
        client = self._client()

        def call() -> Any:
            version = self.version or self._resolve_version(client, tool_slug)
            return client.tools.execute(
                tool_slug,
                arguments,
                connected_account_id=connected_account_id,
                user_id=user_id,
                version=version,
            )

        payload = await self._run(call)
        data = getattr(payload, "data", None)
        error = getattr(payload, "error", None)
        successful = getattr(payload, "successful", None)
        if data is None and isinstance(payload, dict):
            data = payload.get("data")
            error = payload.get("error")
            successful = payload.get("successful")

        if successful is False or error:
            message = error.get("message") if isinstance(error, dict) else (error or "Tool execution failed")
            raise IntegrationError(
                f"{tool_slug}: {_short(message)}",
                provider=tool_slug.split("_")[0].lower(),
                code="tool_failed",
                details=error,
            )
        return data

    async def get_tool(self, tool_slug: str) -> dict[str, Any]:
        client = self._client()
        return _as_dict(await self._run(client.tools.get_raw_composio_tool_by_slug, tool_slug))

    # -------------------------------------------- auth configs + connected accounts
    async def resolve_auth_config(self, toolkit: str) -> dict[str, Any]:
        """Return an enabled auth config for ``toolkit``.

        Auth-config IDs are deliberately **not** configuration: the adapter
        reuses whichever enabled config already exists in the Composio project
        (including a custom one) and creates a Composio-managed config on first
        use when the project has none. This keeps adding connectors purely a
        Composio-dashboard concern.
        """
        client = self._client()

        def call() -> dict[str, Any]:
            response = client.auth_configs.list(toolkit_slug=toolkit, limit=100)
            items = getattr(response, "items", None) or []
            candidates: list[dict[str, Any]] = []
            for item in items:
                data = _as_dict(item)
                nested = data.get("toolkit") or {}
                slug = nested.get("slug") if isinstance(nested, dict) else None
                if data.get("status") == "ENABLED" and (slug in (None, toolkit)):
                    candidates.append(data)
            if candidates:
                # Prefer a Composio-managed config; custom configs are equally usable.
                candidates.sort(key=lambda item: not item.get("is_composio_managed"))
                chosen = candidates[0]
                return {"id": chosen.get("id"), "created": False, "managed": chosen.get("is_composio_managed")}

            created = client.auth_configs.create(
                toolkit=toolkit,
                options={"type": "use_composio_managed_auth"},
            )
            return {"id": getattr(created, "id", None), "created": True, "managed": True}

        resolved = await self._run(call)
        if not resolved.get("id"):
            raise IntegrationError(
                f"Composio did not return an auth config for {toolkit}",
                provider=toolkit,
                code="bad_response",
            )
        return resolved

    async def create_connect_link(
        self,
        *,
        auth_config_id: str,
        user_id: str,
        callback_url: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        client = self._client()
        link = await self._run(
            client.connected_accounts.link,
            user_id,
            auth_config_id,
            callback_url=callback_url,
            alias=alias,
            allow_multiple=True,
        )
        # The SDK returns ``ConnectionRequest``, a plain object whose ``id`` is
        # the connected-account id; it is neither a dict nor a pydantic model,
        # so attribute access is required.
        data = _as_dict(link)
        return {
            "connected_account_id": getattr(link, "id", None) or data.get("connected_account_id"),
            "redirect_url": getattr(link, "redirect_url", None) or data.get("redirect_url"),
            "status": getattr(link, "status", None) or data.get("status"),
        }

    async def list_connected_accounts(
        self,
        *,
        user_id: str | None = None,
        toolkit_slug: str | None = None,
        connected_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        client = self._client()
        kwargs: dict[str, Any] = {"limit": 50, "account_type": "ALL"}
        if user_id:
            kwargs["user_ids"] = [user_id]
        if toolkit_slug:
            kwargs["toolkit_slugs"] = [toolkit_slug]
        if connected_account_id:
            kwargs["connected_account_ids"] = [connected_account_id]
        response = await self._run(client.connected_accounts.list, **kwargs)
        payload = _as_dict(response)
        items = payload.get("items") or []
        return [item if isinstance(item, dict) else _as_dict(item) for item in items]

    async def get_connected_account(self, connected_account_id: str) -> dict[str, Any] | None:
        items = await self.list_connected_accounts(connected_account_id=connected_account_id)
        return items[0] if items else None

    async def delete_connected_account(self, connected_account_id: str) -> None:
        """Delete a connected account and revoke its OAuth grant at the provider.

        A local "disconnect" that only flips a status row leaves the Composio
        credentials usable by the hosted MCP session, so the remote account is
        removed here. Missing accounts raise ``code="not_found"``.
        """
        client = self._client()
        await self._run(
            client.connected_accounts.delete,
            connected_account_id,
            revoke_on_delete=True,
        )

    # ------------------------------------------------------------------- MCP
    async def create_mcp_session(
        self,
        *,
        user_id: str,
        toolkits: list[str],
        manage_connections: bool = True,
    ) -> McpSession:
        """Create (or reuse) a scoped hosted MCP session for the chatbot.

        The returned headers contain the Composio API key: they are for
        server-side use only and must never be returned to a browser.
        """
        cached = self._mcp_sessions.get(user_id)
        if cached and (time.time() - cached.created_at) < 25 * 60:
            return cached

        client = self._client()
        session = await self._run(
            client.sessions.create,
            user_id=user_id,
            toolkits=toolkits,
            manage_connections=manage_connections,
            mcp=True,
        )
        mcp = getattr(session, "mcp", None)
        url = getattr(mcp, "url", None)
        headers = getattr(mcp, "headers", None) or {}
        if not url:
            raise IntegrationError(
                "Composio did not return an MCP endpoint for the session",
                provider="composio",
                code="bad_response",
            )
        record = McpSession(
            session_id=str(getattr(session, "id", "") or ""),
            user_id=user_id,
            toolkits=list(toolkits),
            url=str(url),
            headers={str(k): str(v) for k, v in dict(headers).items() if v},
        )
        self._mcp_sessions[user_id] = record
        return record

    def forget_mcp_session(self, user_id: str) -> None:
        self._mcp_sessions.pop(user_id, None)


composio = ComposioClient()


def extract_email_from_account(account: dict[str, Any]) -> str | None:
    """Pull the mailbox address out of a connected-account payload.

    Composio has changed this shape several times (``state.val``,
    ``connectionParams``, ``data``, ``alias``), so we scan all known locations
    and both snake_case and camelCase keys.
    """
    holders: list[Any] = []
    state = account.get("state")
    if isinstance(state, dict):
        holders.append(state.get("val"))
        holders.append(state)
    for key in ("connectionParams", "connection_params", "data"):
        value = account.get(key)
        if isinstance(value, dict):
            holders.append(value)
    holders.append(account)

    keys = (
        "email",
        "emailAddress",
        "email_address",
        "account_email",
        "identifier",
        "username",
    )
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        for key in keys:
            value = holder.get(key)
            if isinstance(value, str) and "@" in value:
                return value
    for candidate in (account.get("alias"), account.get("display_name")):
        if isinstance(candidate, str) and "@" in candidate:
            return candidate
    return None


def normalize_tool_data(data: Any) -> dict[str, Any]:
    """Composio responses occasionally wrap payloads in ``response_data``."""
    if isinstance(data, dict):
        if isinstance(data.get("response_data"), dict):
            merged = dict(data.get("response_data"))
            merged.setdefault("_envelope", {k: v for k, v in data.items() if k != "response_data"})
            return merged
        return data
    if isinstance(data, list):
        return {"items": data}
    return {}
