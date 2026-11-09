"""FastAPI entrypoint (production-only).

Scheduled ingestion runs through one ``tick`` function. A single API instance
runs it in-process (``SCHEDULER_INPROCESS``, default on); Supabase pg_cron can
drive the same tick over ``/internal/cron/tick`` when the Vault secrets and a
public API URL are configured. The manual "Sync now" endpoint remains for
operators. At boot the process validates every required setting and fails fast
if anything is missing.
"""
from __future__ import annotations

import asyncio
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

_scheduler_task: asyncio.Task | None = None


async def _scheduler_loop() -> None:
    """Run one scheduler tick every ``scheduler_tick_seconds``.

    This calls the same :func:`app.services.scheduler.tick` that Supabase cron
    would call, so there is a single behaviour. A failed tick is logged and the
    loop keeps running; it never crashes the API.
    """
    from .services.scheduler import tick

    interval = max(10, settings.scheduler_tick_seconds)
    log.info("in-process scheduler started (every %ss)", interval)
    while True:
        try:
            async with session() as db:
                result = await tick(db, worker_id="inprocess", max_jobs=5)
            if result.get("enqueued") or result.get("processed"):
                log.info(
                    "scheduler tick: enqueued=%s processed=%s",
                    len(result.get("enqueued") or []),
                    len(result.get("processed") or []),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task
    settings.validate_runtime()
    await init_db()
    log.info(
        "AI Recruiter OS %s | db=postgres | llm=%s | composio=%s | storage=%s | scheduler=%s",
        __version__,
        "on" if settings.llm_enabled else "heuristic",
        "on" if settings.composio_enabled else "off",
        storage.backend,
        "in-process" if settings.scheduler_inprocess else "pg_cron",
    )
    if settings.scheduler_inprocess:
        _scheduler_task = asyncio.create_task(_scheduler_loop(), name="inprocess-scheduler")
    try:
        yield
    finally:
        if _scheduler_task is not None:
            _scheduler_task.cancel()
            await asyncio.gather(_scheduler_task, return_exceptions=True)
            _scheduler_task = None
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
        "scheduler": "in-process" if settings.scheduler_inprocess else "pg_cron",
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
