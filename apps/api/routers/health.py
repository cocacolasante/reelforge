"""/health and /ready endpoints — same behavior as Phase 0 /health plus /ready
which only returns 200 when DB and arq are usable."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_db, get_redis
from apps.api.settings import settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_anthropic_cache: dict[str, float | bool] = {"at": 0.0, "ok": False}


def _ffmpeg_version() -> str | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"ffmpeg version (\S+)", r.stdout)
    return m.group(1) if m else r.stdout.splitlines()[0]


def _whisper_cache_gb() -> float:
    cache = Path(os.environ.get("WHISPER_MODEL_CACHE", "/models/whisper"))
    if not cache.exists():
        return 0.0
    total = 0
    for p in cache.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return round(total / (1024**3), 2)


async def _redis_connected(client) -> bool:
    try:
        return await client.ping()
    except Exception as exc:
        log.warning("redis ping failed: %s", exc)
        return False


async def _anthropic_reachable() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    now = time.monotonic()
    if now - float(_anthropic_cache["at"]) < 60.0:
        return bool(_anthropic_cache["ok"])

    def _probe() -> bool:
        try:
            from anthropic import Anthropic

            Anthropic().models.list(limit=1)
            return True
        except Exception as exc:
            log.warning("anthropic reachability check failed: %s", exc)
            return False

    ok = await asyncio.to_thread(_probe)
    _anthropic_cache["at"] = now
    _anthropic_cache["ok"] = ok
    return ok


@router.get("/health")
async def health(request: Request) -> dict:
    redis_ok, anthropic_ok = await asyncio.gather(
        _redis_connected(request.app.state.redis),
        _anthropic_reachable(),
    )
    return {
        "status": "ok" if redis_ok else "degraded",
        "ffmpeg": _ffmpeg_version() or "missing",
        "redis": "connected" if redis_ok else "disconnected",
        "whisper_models_cache_gb": _whisper_cache_gb(),
        "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "anthropic_reachable": anthropic_ok,
        "data_dir": str(settings.data_dir),
        "environment": settings.environment,
    }


@router.get("/ready")
async def ready(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        return {"ready": False, "reason": f"db: {exc}"}
    try:
        ok = await request.app.state.redis.ping()
        if not ok:
            return {"ready": False, "reason": "redis: ping returned false"}
    except Exception as exc:
        return {"ready": False, "reason": f"redis: {exc}"}
    return {"ready": True}
