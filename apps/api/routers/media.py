"""Media streaming: reel preview, export download, scene thumbnails, caption preview."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import subprocess
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.errors import ApiError
from apps.api.settings import settings
from apps.api.streaming import stream_file_with_range
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.models import AnalysisReport, ReelSelection

log = logging.getLogger(__name__)
router = APIRouter(tags=["media"])

_MT = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
}


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\-\s_]", "", s).strip()
    s = re.sub(r"\s+", "-", s).lower()
    return s[:80] or "reel"


# ---------------------------------------------------------------------------
# Reel preview + caption preview
# ---------------------------------------------------------------------------


@router.get("/reels/{reel_id}/preview")
async def preview_reel(
    reel_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    mezz = working_dir_for(r.asset_id) / "reels" / reel_id / "mezzanine.mp4"
    if not mezz.exists():
        raise ApiError(404, "MEZZANINE_NOT_READY", "compose has not completed")
    return await stream_file_with_range(
        mezz,
        request,
        media_type="video/mp4",
        cache_control="private, no-cache",
    )


# ---- caption preview ----

_caption_ip_buckets: dict[str, deque[float]] = {}


def _rate_limit_ok(ip: str) -> bool:
    window = 60.0
    limit = settings.caption_preview_rpm_per_ip
    now = time.monotonic()
    bucket = _caption_ip_buckets.setdefault(ip, deque())
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


@router.get("/reels/{reel_id}/caption-preview")
async def caption_preview(
    reel_id: str,
    request: Request,
    style: str = Query("static"),
    t: float = Query(0.0, ge=0.0),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if style not in ("off", "static", "karaoke"):
        raise ApiError(400, "INVALID_CONFIG", f"unknown caption style {style!r}")
    ip = request.client.host if request.client else "anon"
    if not _rate_limit_ok(ip):
        raise ApiError(429, "INVALID_CONFIG", "rate limit exceeded for caption preview")

    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    asset = await db.get(dbmod.Asset, r.asset_id)
    if asset is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {r.asset_id} not found")

    wd = working_dir_for(asset.id)
    analysis_path = wd / "analysis.json"
    if not analysis_path.exists():
        raise ApiError(409, "ANALYSIS_NOT_READY", "analysis.json missing")

    # Build a cached path
    source_mtime = int(Path(asset.path).stat().st_mtime)
    key = hashlib.sha256(
        f"{reel_id}|{style}|{t:.3f}|{source_mtime}".encode("utf-8")
    ).hexdigest()[:24]
    cache_dir = wd / "caption_previews"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{key}.png"
    if cache_path.exists():
        return FileResponse(
            cache_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # Build an ephemeral 1-event ASS file for this style
    analysis = AnalysisReport.model_validate_json(analysis_path.read_text())
    reels_path = wd / "reels.json"
    selection = ReelSelection.model_validate_json(reels_path.read_text())
    reel_obj = next((rr for rr in selection.reels if rr.candidate_id == reel_id), None)
    if reel_obj is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not in reels.json")

    source_time = reel_obj.start_sec + max(0.0, t)
    source_time = min(source_time, reel_obj.end_sec - 0.05)

    # Minimal: render a single frame with captions burned in via subtitles filter.
    # For simplicity we use the compose pipeline's existing ASS generation:
    from reelforge_core.compose.captions import build_captions
    from reelforge_core.models import CaptionStyle, ComposeConfig

    cfg = ComposeConfig(captions=CaptionStyle(mode=style))  # type: ignore[arg-type]
    temp_reel_dir = cache_dir / f"_tmp_{key}"
    temp_reel_dir.mkdir(parents=True, exist_ok=True)
    ass_path = build_captions(reel_obj, analysis, cfg, temp_reel_dir)

    w, h = cfg.resolution
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1"
    )
    if style != "off" and analysis.transcript is not None:
        vf += (
            f",subtitles={ass_path}:fontsdir=/usr/share/fonts/truetype/inter"
        )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{source_time:.3f}",
        "-i",
        asset.path,
        "-vf",
        vf,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(cache_path),
    ]
    try:
        await asyncio.wait_for(
            asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True),
            timeout=settings.caption_preview_timeout_s,
        )
    except asyncio.TimeoutError:
        raise ApiError(504, "INTERNAL_ERROR", "caption preview timed out")
    except subprocess.CalledProcessError as exc:
        raise ApiError(
            500,
            "INTERNAL_ERROR",
            f"ffmpeg failed: {exc.stderr.decode('utf-8','replace')[-300:]}",
        )
    return FileResponse(
        cache_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# Export download
# ---------------------------------------------------------------------------


@router.get("/exports/{export_id}/download")
async def download_export(
    export_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    e = await db.get(dbmod.Export, export_id)
    if e is None or not e.output_path:
        raise ApiError(404, "EXPORT_NOT_FOUND", f"export {export_id} has no output yet")
    out = Path(e.output_path)
    if not out.exists():
        raise ApiError(
            404, "EXPORT_NOT_FOUND", f"export file missing on disk: {out}"
        )
    ext = out.suffix.lstrip(".").lower()
    media_type = _MT.get(ext, "application/octet-stream")

    reel = await db.get(dbmod.Reel, e.reel_id)
    safe = _slugify(reel.title if reel else e.reel_id)
    filename = f"{safe}-{e.preset_id}.{ext}"

    return await stream_file_with_range(
        out,
        request,
        media_type=media_type,
        filename_for_download=filename,
        cache_control="private, max-age=86400",
    )


# ---------------------------------------------------------------------------
# Scene thumbnails
# ---------------------------------------------------------------------------


@router.get("/assets/{asset_id}/thumbnails/{scene_index}")
async def get_scene_thumbnail(
    asset_id: str, scene_index: int, db: AsyncSession = Depends(get_db)
) -> Response:
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    thumb = working_dir_for(asset_id) / "thumbs" / f"scene_{scene_index:04d}.jpg"
    if not thumb.exists():
        raise ApiError(
            404, "ANALYSIS_NOT_READY", f"thumbnail for scene {scene_index} missing"
        )
    return FileResponse(
        thumb,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/assets/{asset_id}/photo")
async def get_photo(
    asset_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """Serve a photo asset itself (for pickers and previews).

    Photos have no scene thumbnails — they never go through analysis — so the
    original file is what the UI displays.
    """
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    if a.kind != "photo":
        raise ApiError(400, "INVALID_CONFIG", f"asset {asset_id} is not a photo")
    path = Path(a.path)
    if not path.exists():
        raise ApiError(404, "NOT_FOUND", "photo file missing on disk")
    suffix = path.suffix.lower().lstrip(".")
    media_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tif": "image/tiff",
        "tiff": "image/tiff",
    }.get(suffix, "application/octet-stream")
    return await stream_file_with_range(
        path,
        request,
        media_type=media_type,
        cache_control="public, max-age=86400",
    )
