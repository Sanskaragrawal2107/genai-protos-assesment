"""Application settings (12-factor, .env driven).

This service is **production-only**: there is no demo mode, no SQLite and no
local-disk fallback. Every value that the runtime truly needs comes from the
environment and is validated at boot by :meth:`Settings.validate_runtime` so the
process fails fast and loudly instead of silently degrading.

Running locally is fully supported — you just point the process at the real
Supabase project (auth, Postgres, Storage) and real Composio toolkits, exactly
as production would.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

API_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = API_DIR.parent.parent

# Values that must never be accepted as production secrets.
_INSECURE_DEFAULTS = {
    "",
    "change-me-internal-token",
    "dev-internal-token",
    "test-token",
    "changeme",
    "secret",
}


def _sanitize_no_proxy() -> None:
    """Drop bracketed IPv6 entries from NO_PROXY.

    httpx 0.28.1 parses ``[::1]`` as a host with an invalid port and raises
    ``InvalidURL`` while constructing *any* client. Windows often exports
    exactly that form, so normalise it once before clients are created.
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parts = [part.strip() for part in raw.split(",")]
        os.environ[key] = ",".join(
            part for part in parts if part and not (part.startswith("[") and part.endswith("]"))
        )


_sanitize_no_proxy()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", API_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Prioritize workspace .env over ambient OS environment variables
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    # runtime
    web_origin: str = ""
    log_level: str = "INFO"
    # Shared secret for cron/worker -> API calls (Authorization: Bearer ...).
    api_internal_token: str = ""
    # Optional phone number used when the workspace profile has none. The
    # assistant writes the [Phone] token; the server fills it from the saved
    # company phone first, then this fallback. Empty = omit the phone line
    # rather than fabricate a number.
    company_phone_fallback: str = ""

    # database — Supabase Postgres only (asyncpg driver).
    database_url: str = ""

    # supabase
    supabase_url: str = ""
    # Server-only. Never sent to the browser.
    supabase_service_role_key: str = ""
    # JWT secret from Supabase -> Settings -> API (used to verify access tokens).
    supabase_jwt_secret: str = ""
    supabase_storage_bucket: str = "resumes"

    # llm — any OpenAI-compatible endpoint. Blank runs fully deterministically.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1
    # Reasoning budget for gpt-5*/o-series models (ignored by other models).
    # The provider default spent 30s+ per agent call on gpt-5-nano; "low" keeps
    # the same tool choices at a fraction of the latency. Empty = provider default.
    llm_reasoning_effort: str = "low"
    # Streaming "thinking" for reasoning models. ``auto`` asks the OpenAI
    # Responses API for a human-readable reasoning summary and streams it to the
    # chat UI (the raw chain of thought is never exposed by the provider). Set to
    # "off" to stay on Chat Completions and show no thinking panel.
    llm_reasoning_summary: str = "auto"

    # composio — the only external tool layer (Gmail + Microsoft Teams).
    # Auth configs are resolved (or created) per toolkit at connect time, so no
    # auth-config IDs belong in the environment.
    composio_api_key: str = ""
    composio_toolkit_version: str = ""
    composio_base_url: str = "https://backend.composio.dev"
    composio_user_prefix: str = "airecruiter"

    # github — optional; raises the anonymous rate limit.
    github_token: str = ""

    # leetcode — the public GraphQL endpoint is the primary evidence source. The
    # hosted MCP server is optional and only tried as a last resort when
    # LEETCODE_MCP_FALLBACK is enabled and GraphQL is unavailable.
    leetcode_mcp_url: str = ""
    leetcode_mcp_token: str = ""
    leetcode_mcp_timeout_seconds: float = 30.0
    leetcode_mcp_fallback: bool = False

    # MCP server (apps/mcp). Resolved server-side, never from model input.
    mcp_workspace_id: str = ""
    mcp_user_id: str = ""

    # scheduling / ingestion tuning
    sync_default_interval_minutes: int = 30
    sync_max_attempts: int = 4
    sync_overlap_minutes: int = 60
    sync_max_resume_bytes: int = 10 * 1024 * 1024
    # Run the scheduler tick inside the API process. This is the working path for
    # a single-instance deployment (and local dev); set SCHEDULER_INPROCESS=false
    # when Supabase pg_cron is fully wired up (Vault secrets + public API URL).
    scheduler_inprocess: bool = True
    scheduler_tick_seconds: int = 60
    default_timezone: str = "Asia/Kolkata"
    working_hours_start: int = 9
    working_hours_end: int = 19

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def composio_enabled(self) -> bool:
        return bool(self.composio_api_key.strip())

    @property
    def leetcode_mcp_enabled(self) -> bool:
        return bool(self.leetcode_mcp_url.strip() and self.leetcode_mcp_token.strip())

    @property
    def cors_origins(self) -> list[str]:
        origins = {
            "http://localhost:3000", "http://127.0.0.1:3000",
            "http://localhost:3001", "http://127.0.0.1:3001",
            "http://localhost:3100", "http://127.0.0.1:3100",
            "http://localhost:8000", "http://127.0.0.1:8000",
        }
        if self.web_origin and self.web_origin.strip():
            origins.add(self.web_origin.strip().rstrip("/"))
        return sorted(origins)

    # ------------------------------------------------------------- validation
    def missing_required(self) -> list[str]:
        """Return the names of required environment variables that are absent.

        ``DATABASE_URL`` must be an async Postgres (asyncpg) URL; anything that
        points at SQLite or another synchronous driver is rejected here so the
        process can never silently fall back to a local database.
        """
        problems: list[str] = []

        if not self.database_url.strip():
            problems.append("DATABASE_URL (required: postgresql+asyncpg://...)")
        elif not self.database_url.startswith("postgresql+asyncpg://"):
            problems.append(
                "DATABASE_URL must be a Postgres asyncpg URL "
                "(postgresql+asyncpg://...); SQLite and sync drivers are not supported"
            )

        for name, value in (
            ("SUPABASE_URL", self.supabase_url),
            ("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key),
            ("API_INTERNAL_TOKEN", self.api_internal_token),
            ("WEB_ORIGIN", self.web_origin),
        ):
            if not value.strip():
                problems.append(f"{name} is required")

        if self.api_internal_token.strip() in _INSECURE_DEFAULTS:
            problems.append("API_INTERNAL_TOKEN must be a strong, unique secret")

        return problems

    def validate_runtime(self) -> None:
        """Raise :class:`RuntimeError` listing every missing/invalid setting."""
        problems = self.missing_required()
        if problems:
            bullet = "\n".join(f"  - {item}" for item in problems)
            raise RuntimeError(
                "AI Recruiter OS is misconfigured. Fix the following and restart:\n"
                f"{bullet}\n"
                "See .env.example for the full list of production settings."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
