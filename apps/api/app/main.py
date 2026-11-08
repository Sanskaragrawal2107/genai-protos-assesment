"""FastAPI entrypoint (production-only).

There is no in-process scheduler and no demo mode: scheduled ingestion runs via
pg_cron -> Supabase Edge Functions -> ``/internal/cron/tick``. The manual
"Sync now" endpoint remains for operators. At boot the process validates every
required setting and fails fast if anything is missing.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .composio import IntegrationError
from .config import settings
from .db import init_db, reset_engine, session
from .http import close_client
from .routers import api as api_router
from .routers import internal as internal_router
from .services.storage import storage

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("recruiter")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    await init_db()
    log.info(
        "AI Recruiter OS %s | db=postgres | llm=%s | composio=%s | storage=%s | scheduler=pg_cron",
        __version__,
        "on" if settings.llm_enabled else "heuristic",
        "on" if settings.composio_enabled else "off",
        storage.backend,
    )
    try:
        yield
    finally:
        await close_client()
        await reset_engine()


app = FastAPI(
    title="AI Recruiter OS",
    version=__version__,
    description="AI-powered recruitment operating system: Gmail ingestion, resume parsing, "
    "explainable candidate evaluation, GitHub/LeetCode verification and interview scheduling "
    "through Microsoft Teams meetings with Gmail invitations.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(api_router.router)
app.include_router(internal_router.router)


@app.get("/health")
async def health() -> dict[str, object]:
    db_ok = True
    try:
        async with session() as db:
            await db.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "database": db_ok,
        "llm": settings.llm_enabled,
        "composio": settings.composio_enabled,
        "storage": storage.backend,
        "scheduler": "pg_cron",
    }


@app.exception_handler(IntegrationError)
async def integration_error_handler(request: Request, exc: IntegrationError) -> JSONResponse:
    log.warning("integration error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "detail": str(exc),
            "provider": exc.provider,
            "code": exc.code,
            "hint": _hint_for(exc),
        },
    )


def _hint_for(exc: IntegrationError) -> str | None:
    if exc.code == "not_connected":
        return "Open Settings → Integrations and connect the required account."
    if exc.code == "not_configured":
        return "Add the provider credentials to .env (see .env.example) and restart the API."
    if exc.code == "auth_required":
        return "The connected account lost authorization. Reconnect it in Settings → Integrations."
    return None
