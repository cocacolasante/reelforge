"""Shared throttled progress writer for arq jobs.

Writes stage/stage_progress/overall/message to a Redis hash, capped at one
update every 500 ms (and always emits terminal events)."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from reelforge_core.models import ProgressEvent

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]

MIN_EMIT_INTERVAL_S = 0.5
HASH_TTL_S = 3600


def make_throttled_progress_writer(redis: Any, job_id: str) -> ProgressCallback:
    """Build an async progress callback that writes to `job:{job_id}:progress`.

    Emits terminal events (stage == done/error or overall >= 1.0) immediately,
    throttling non-terminal events to at most once per 500 ms.
    """
    last_emit = 0.0

    async def on_progress(evt: ProgressEvent) -> None:
        nonlocal last_emit
        now = time.monotonic()
        terminal = (
            evt.overall_progress >= 1.0
            or evt.stage_progress >= 1.0
            or evt.stage in {"done", "error"}
        )
        if not terminal and now - last_emit < MIN_EMIT_INTERVAL_S:
            return
        last_emit = now
        await redis.hset(
            f"job:{job_id}:progress",
            mapping={
                "stage": evt.stage,
                "stage_progress": f"{evt.stage_progress:.4f}",
                "overall": f"{evt.overall_progress:.4f}",
                "message": evt.message or "",
            },
        )
        await redis.expire(f"job:{job_id}:progress", HASH_TTL_S)

    return on_progress


async def write_terminal(redis: Any, job_id: str, stage: str, message: str) -> None:
    """Write an immediate terminal state (done/error) outside of the progress loop."""
    await redis.hset(
        f"job:{job_id}:progress",
        mapping={
            "stage": stage,
            "stage_progress": "1",
            "overall": "1",
            "message": message,
        },
    )
    await redis.expire(f"job:{job_id}:progress", HASH_TTL_S)
