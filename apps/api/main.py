"""ReelForge API. FastAPI app factory with lifespan + middleware + routers.

No heavy work lives here — every long-running task is enqueued to arq. The one
exception is the synchronous caption-preview endpoint (time-boxed, cached).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import redis.asyncio as redis_async
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from apps.api import db as dbmod
from apps.api.middleware import RequestIdMiddleware, configure_api_logging
from apps.api.schemas.errors import ApiError, api_error_response
from apps.api.settings import settings
from reelforge_core.paths import DATA_DIR

log = logging.getLogger(__name__)

REDIS_URL = settings.redis_url
ANTHROPIC_CHECK_TTL_S = 60.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_api_logging(settings.log_level)
    log.info("api startup: db=%s redis=%s", settings.database_url, settings.redis_url)

    await dbmod.init_db(settings.database_url)
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.redis = redis_async.from_url(settings.redis_url, decode_responses=True)

    # Reset jobs that were running when the API/worker died. See spec §5/§6.
    await _reset_interrupted_jobs(app)

    # Start background upload-cleanup loop.
    app.state.bg_tasks = [
        asyncio.create_task(_purge_abandoned_uploads_loop()),
    ]

    try:
        yield
    finally:
        for t in app.state.bg_tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        await app.state.redis.aclose()
        await app.state.arq_pool.aclose()
        await dbmod.dispose_db()


async def _reset_interrupted_jobs(app: FastAPI) -> None:
    """Mark any job left in (queued|running) state as failed if arq has no
    record of it. Since we can't distinguish 'worker crashed' from 'API just
    booted with fresh Redis', we take the conservative path: on startup,
    assume any non-terminal DB row is interrupted unless arq confirms it's
    still alive. For Phase 5 simplicity we treat *all* non-terminal jobs as
    interrupted — the queue is ephemeral and we don't resume."""
    async with dbmod.db_state.sessionmaker() as session:
        rows = (
            await session.execute(
                select(dbmod.Job).where(dbmod.Job.status.in_(("queued", "running")))
            )
        ).scalars().all()
        if not rows:
            return
        await session.execute(
            update(dbmod.Job)
            .where(dbmod.Job.status.in_(("queued", "running")))
            .values(
                status="failed",
                error_message="interrupted by restart",
                finished_at=datetime.now(timezone.utc),
                stage="error",
                message="interrupted by restart",
            )
        )
        await session.commit()
        log.info("marked %d interrupted jobs as failed", len(rows))


async def _purge_abandoned_uploads_loop() -> None:
    """Once an hour, abort upload sessions older than 24h and delete their parts."""
    from datetime import timedelta

    while True:
        try:
            async with dbmod.db_state.sessionmaker() as session:
                threshold = datetime.now(timezone.utc) - timedelta(hours=24)
                rows = (
                    await session.execute(
                        select(dbmod.UploadSession).where(
                            dbmod.UploadSession.status == "active",
                            dbmod.UploadSession.created_at < threshold,
                        )
                    )
                ).scalars().all()
                for s in rows:
                    shutil.rmtree(s.parts_dir, ignore_errors=True)
                    s.status = "aborted"
                    s.completed_at = datetime.now(timezone.utc)
                if rows:
                    log.info("purged %d abandoned upload sessions", len(rows))
                    await session.commit()
        except Exception:  # pragma: no cover
            log.exception("purge_abandoned_uploads_loop iteration failed")
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="ReelForge API",
        version="0.5.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "Content-Range", "Accept-Ranges"],
    )
    app.add_middleware(RequestIdMiddleware)

    # Register routers (late import so settings/db are available at import time).
    from apps.api.routers import (
        admin as admin_router,
        compose as compose_router,
        cost as cost_router,
        exports as exports_router,
        health as health_router,
        jobs as jobs_router,
        media as media_router,
        music as music_router,
        pipeline as pipeline_router,
        projects as projects_router,
        reels as reels_router,
        uploads as uploads_router,
    )

    # Root-level
    app.include_router(health_router.router)
    # /api/v1
    api_v1 = "/api/v1"
    app.include_router(projects_router.router, prefix=api_v1)
    app.include_router(uploads_router.router, prefix=api_v1)
    app.include_router(pipeline_router.router, prefix=api_v1)
    app.include_router(compose_router.router, prefix=api_v1)
    app.include_router(reels_router.router, prefix=api_v1)
    app.include_router(exports_router.router, prefix=api_v1)
    app.include_router(jobs_router.router, prefix=api_v1)
    app.include_router(media_router.router, prefix=api_v1)
    app.include_router(music_router.router, prefix=api_v1)
    app.include_router(cost_router.router, prefix=api_v1)
    app.include_router(admin_router.router, prefix=api_v1)
    from apps.api.routers import transcripts as transcripts_router  # noqa: E402

    app.include_router(transcripts_router.router, prefix=api_v1)
    from apps.api.routers import montages as montages_router  # noqa: E402

    app.include_router(montages_router.router, prefix=api_v1)

    from apps.api.routers import social as social_router  # noqa: E402

    app.include_router(social_router.router, prefix=api_v1)

    # Exception handlers
    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        return api_error_response(
            exc.status_code, exc.code, exc.message, request_id=rid, **exc.details
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled exception", extra={"request_id": rid})
        # Don't leak internals in prod.
        message = str(exc) if settings.environment == "development" else "internal error"
        return api_error_response(500, "INTERNAL_ERROR", message, request_id=rid)

    return app


app = create_app()
