"""Music library listing + audio preview + user uploads."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

from apps.api.schemas.common import MusicTrackOut
from apps.api.schemas.errors import ApiError
from apps.api.settings import settings
from apps.api.streaming import stream_file_with_range
from reelforge_core.compose.music import load_music_library

log = logging.getLogger(__name__)

router = APIRouter(tags=["music"])

_AUDIO_MT = {
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
}
_UPLOAD_MAX_BYTES = 30 * 1024 * 1024  # 30 MiB
_USER_MUSIC_DIR = settings.data_dir / "music"
_USER_MANIFEST = _USER_MUSIC_DIR / "manifest.json"


def _to_track_out(track) -> MusicTrackOut:
    return MusicTrackOut(
        id=track.id,
        path=track.path,
        source=track.source,
        bpm=track.bpm,
        mood=track.mood,
        duration_sec=track.duration_sec,
        license=track.license,
        attribution=track.attribution,
    )


@router.get("/music")
async def list_music() -> dict:
    tracks = load_music_library()
    return {"tracks": [_to_track_out(t).model_dump() for t in tracks]}


@router.get("/music/{track_id}", response_model=MusicTrackOut)
async def get_music(track_id: str) -> MusicTrackOut:
    tracks = load_music_library()
    match = next((t for t in tracks if t.id == track_id), None)
    if match is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"music track {track_id} not found")
    return _to_track_out(match)


@router.get("/music/{track_id}/audio")
async def stream_music(track_id: str, request: Request) -> Response:
    tracks = load_music_library()
    match = next((t for t in tracks if t.id == track_id), None)
    if match is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"music track {track_id} not found")
    p = Path(match.path)
    if not p.exists():
        raise ApiError(404, "ASSET_NOT_FOUND", f"music file missing on disk: {p}")
    media_type = _AUDIO_MT.get(p.suffix.lstrip(".").lower(), "audio/mpeg")
    return await stream_file_with_range(
        p, request, media_type=media_type, cache_control="public, max-age=86400"
    )


# ---------------------------------------------------------------------------
# User uploads
# ---------------------------------------------------------------------------


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\-_]", "-", s.strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "track")[:40]


def _read_user_manifest() -> dict:
    if not _USER_MANIFEST.exists():
        return {"tracks": []}
    try:
        return json.loads(_USER_MANIFEST.read_text())
    except Exception:
        return {"tracks": []}


def _write_user_manifest(data: dict) -> None:
    _USER_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _USER_MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(_USER_MANIFEST)


def _probe_audio(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise ApiError(400, "UPLOAD_UNSUPPORTED_TYPE", f"ffprobe rejected file: {r.stderr.strip()}")
    return json.loads(r.stdout)


@router.post("/music/uploads", response_model=MusicTrackOut, status_code=201)
async def upload_music(
    file: UploadFile = File(...),
    title: str = Form(...),
    mood: str = Form("neutral"),
    license: str = Form("CC0"),
    bpm: str | None = Form(None),
    attribution: str | None = Form(None),
    scope: str = Form("global"),
) -> MusicTrackOut:
    # MIME check — allow common audio containers.
    if not (file.content_type or "").startswith("audio/"):
        raise ApiError(
            400,
            "UPLOAD_UNSUPPORTED_TYPE",
            f"content_type {file.content_type!r} is not audio/*",
        )
    ext = Path(file.filename or "").suffix.lstrip(".").lower() or "mp3"
    if ext not in _AUDIO_MT:
        raise ApiError(400, "UPLOAD_UNSUPPORTED_TYPE", f"unsupported extension .{ext}")

    _USER_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    track_id = f"user-{_slugify(title)}-{uuid.uuid4().hex[:8]}"
    dest = _USER_MUSIC_DIR / f"{track_id}.{ext}"
    # Stream to disk
    received = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            received += len(chunk)
            if received > _UPLOAD_MAX_BYTES:
                dest.unlink(missing_ok=True)
                raise ApiError(
                    413,
                    "UPLOAD_TOO_LARGE",
                    f"audio upload exceeds {_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB limit",
                )
            out.write(chunk)

    # Verify with ffprobe
    probe = _probe_audio(dest)
    audio_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        dest.unlink(missing_ok=True)
        raise ApiError(400, "UPLOAD_UNSUPPORTED_TYPE", "file has no audio stream")
    duration = float(probe.get("format", {}).get("duration") or audio_streams[0].get("duration") or 0.0)

    bpm_val: int | None = None
    if bpm:
        try:
            bpm_val = int(bpm)
        except ValueError:
            bpm_val = None

    entry = {
        "id": track_id,
        "path": str(dest),
        "bpm": bpm_val,
        "mood": mood if mood in {
            "calm", "tense", "joyful", "somber", "energetic",
            "mysterious", "romantic", "triumphant", "melancholic", "neutral",
        } else "neutral",
        "duration_sec": round(duration, 3),
        "license": license,
        "attribution": attribution,
        "scope": scope,
    }
    manifest = _read_user_manifest()
    manifest["tracks"].append(entry)
    _write_user_manifest(manifest)

    return MusicTrackOut(
        id=entry["id"],
        path=entry["path"],
        source="user",
        bpm=entry["bpm"],
        mood=entry["mood"],
        duration_sec=entry["duration_sec"],
        license=entry["license"],
        attribution=entry["attribution"],
    )


@router.delete("/music/{track_id}", status_code=204)
async def delete_music(track_id: str) -> None:
    manifest = _read_user_manifest()
    tracks = manifest.get("tracks", [])
    keep: list[dict] = []
    removed: dict | None = None
    for t in tracks:
        if t["id"] == track_id:
            removed = t
            continue
        keep.append(t)
    if removed is None:
        # If the id matches a bundled track, reject with 403.
        bundled = [t for t in load_music_library() if t.source == "bundled" and t.id == track_id]
        if bundled:
            raise ApiError(403, "INVALID_CONFIG", "bundled tracks cannot be deleted")
        raise ApiError(404, "ASSET_NOT_FOUND", f"music track {track_id} not found")
    manifest["tracks"] = keep
    _write_user_manifest(manifest)
    try:
        Path(removed["path"]).unlink(missing_ok=True)
    except OSError:
        pass
