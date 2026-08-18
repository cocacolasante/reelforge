"""arq worker entrypoint."""

from __future__ import annotations

import logging
import os

from arq.connections import RedisSettings

from apps.worker.jobs import (
    analyze_asset,
    compile_montage_job,
    compose_reel_job,
    export_reel_job,
    publish_reel_job,
    select_reels_job,
)
from apps.worker.logging_config import configure_logging
from reelforge_core.db import init_db

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


async def ping(ctx: dict, payload: str = "ping") -> str:
    log.info("ping job: %s", payload)
    return f"pong:{payload}"


REQUIRED_ENCODERS = ("libx264", "libx265", "prores_ks", "aac", "pcm_s16le")


def _verify_ffmpeg_encoders() -> None:
    """Fail loudly on worker boot if the image is missing a codec we rely on."""
    import subprocess

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.error("ffmpeg not available: %s", exc)
        raise
    missing = [enc for enc in REQUIRED_ENCODERS if enc not in result.stdout]
    if missing:
        msg = f"ffmpeg image is missing required encoders: {missing}"
        log.error(msg)
        raise RuntimeError(msg)
    log.info("ffmpeg encoders verified: %s", list(REQUIRED_ENCODERS))


async def on_startup(ctx: dict) -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    init_db()
    _verify_ffmpeg_encoders()
    log.info("worker startup complete")


async def on_shutdown(ctx: dict) -> None:
    log.info("worker shutdown")


class WorkerSettings:
    functions = [
        ping,
        analyze_asset,
        select_reels_job,
        compose_reel_job,
        export_reel_job,
        compile_montage_job,
        publish_reel_job,
    ]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    # Lets the API abort in-flight jobs (e.g. when a source clip is deleted
    # mid-analysis) instead of leaving FFmpeg/Whisper working on dead files.
    allow_abort_jobs = True
    # analyze_asset legitimately runs up to an hour on long sources; compose +
    # export are FFmpeg-heavy (CPU-bound). Cap concurrent jobs at 2 so we don't
    # thrash. Scale horizontally with `docker compose up --scale worker=N` for
    # more parallelism.
    job_timeout = 3600
    max_jobs = 2
    keep_result = 3600
    on_startup = on_startup
    on_shutdown = on_shutdown
