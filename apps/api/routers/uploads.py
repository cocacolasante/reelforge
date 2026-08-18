"""Chunked resumable uploads. Raw PUT per chunk, streamed straight to disk."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.common import (
    AssetOut,
    UploadSessionCreate,
    UploadSessionOut,
)
from apps.api.schemas.errors import ApiError
from apps.api.settings import settings
from reelforge_core.ingest import asset_to_dict, probe

log = logging.getLogger(__name__)
router = APIRouter(tags=["uploads"])


def _parts_dir(upload_id: str) -> Path:
    return settings.data_dir / "uploads" / ".parts" / upload_id


def _final_path(asset_id: str, filename: str) -> Path:
    ext = Path(filename).suffix.lstrip(".").lower() or "mp4"
    return settings.data_dir / "uploads" / f"{asset_id}.{ext}"


def _received_chunk_indices(s: dbmod.UploadSession) -> list[int]:
    if s.status != "active":
        return []
    parts_dir = Path(s.parts_dir)
    if not parts_dir.is_dir():
        return []
    out: list[int] = []
    for p in parts_dir.glob("chunk_*.bin"):
        try:
            out.append(int(p.stem.split("_")[-1]))
        except ValueError:  # pragma: no cover - foreign file in parts dir
            continue
    return sorted(out)


def _to_session_out(s: dbmod.UploadSession) -> UploadSessionOut:
    return UploadSessionOut(
        id=s.id,
        project_id=s.project_id,
        filename=s.filename,
        content_type=s.content_type,
        total_bytes=s.total_bytes,
        received_bytes=s.received_bytes,
        chunk_size=s.chunk_size,
        status=s.status,  # type: ignore[arg-type]
        asset_id=s.asset_id,
        created_at=s.created_at,
        received_chunk_indices=_received_chunk_indices(s),
    )


@router.post(
    "/projects/{project_id}/uploads",
    response_model=UploadSessionOut,
    status_code=201,
)
async def create_upload(
    project_id: str,
    body: UploadSessionCreate,
    db: AsyncSession = Depends(get_db),
) -> UploadSessionOut:
    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")

    ctype = body.content_type.lower()
    if not (ctype.startswith("video/") or ctype.startswith("image/")):
        raise ApiError(
            400,
            "UPLOAD_UNSUPPORTED_TYPE",
            f"content_type {body.content_type!r} is not a video/* or image/* MIME type",
        )
    limit_bytes = int(settings.max_upload_gb * (1024**3))
    if body.total_bytes <= 0 or body.total_bytes > limit_bytes:
        raise ApiError(
            413,
            "UPLOAD_TOO_LARGE",
            f"upload exceeds {settings.max_upload_gb:.1f} GB limit",
            limit_gb=settings.max_upload_gb,
            attempted_gb=round(body.total_bytes / (1024**3), 2),
        )

    chunk_size = body.chunk_size or settings.default_chunk_mb * 1024 * 1024
    if chunk_size < 64 * 1024:
        raise ApiError(400, "INVALID_CONFIG", "chunk_size must be at least 65536 bytes")

    session = dbmod.UploadSession(
        project_id=project_id,
        filename=body.filename,
        content_type=body.content_type,
        total_bytes=body.total_bytes,
        chunk_size=chunk_size,
        parts_dir=str(_parts_dir("pending")),  # placeholder; rewritten below
    )
    db.add(session)
    await db.flush()
    parts_dir = _parts_dir(session.id)
    parts_dir.mkdir(parents=True, exist_ok=True)
    session.parts_dir = str(parts_dir)
    await db.commit()
    return _to_session_out(session)


@router.get("/uploads/{upload_id}", response_model=UploadSessionOut)
async def get_upload(
    upload_id: str, db: AsyncSession = Depends(get_db)
) -> UploadSessionOut:
    s = await db.get(dbmod.UploadSession, upload_id)
    if s is None:
        raise ApiError(404, "UPLOAD_SESSION_NOT_FOUND", f"upload {upload_id} not found")
    return _to_session_out(s)


@router.put("/uploads/{upload_id}/chunks/{chunk_index}", response_model=UploadSessionOut)
async def put_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UploadSessionOut:
    s = await db.get(dbmod.UploadSession, upload_id)
    if s is None:
        raise ApiError(404, "UPLOAD_SESSION_NOT_FOUND", f"upload {upload_id} not found")
    if s.status != "active":
        raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", f"upload status={s.status}")

    content_length = request.headers.get("content-length")
    if not content_length or not content_length.isdigit():
        raise ApiError(411, "INVALID_CONFIG", "Content-Length header is required")
    declared = int(content_length)

    chunks_total = (s.total_bytes + s.chunk_size - 1) // s.chunk_size
    if chunk_index < 0 or chunk_index >= chunks_total:
        raise ApiError(
            400,
            "UPLOAD_CHUNK_OUT_OF_ORDER",
            f"chunk_index {chunk_index} is outside [0, {chunks_total})",
        )
    is_last = chunk_index == chunks_total - 1
    expected = (
        s.chunk_size
        if not is_last
        else s.total_bytes - s.chunk_size * (chunks_total - 1)
    )
    if declared != expected:
        raise ApiError(
            400,
            "INVALID_CONFIG",
            f"chunk size mismatch: got {declared}, expected {expected}",
        )

    # The parts dir can vanish out from under an active session (manual
    # cleanup, crash between purge steps). Recreate it so a resuming client
    # re-uploads gracefully instead of hitting an unhandled 500.
    Path(s.parts_dir).mkdir(parents=True, exist_ok=True)
    chunk_path = Path(s.parts_dir) / f"chunk_{chunk_index:08d}.bin"
    tmp_path = chunk_path.with_suffix(".tmp")

    # Stream body to disk in 64 KiB chunks — do NOT buffer in memory.
    written = 0
    async with await _aopen_write(tmp_path) as f:
        async for piece in request.stream():
            if not piece:
                continue
            await f.write(piece)
            written += len(piece)
            if written > expected:
                break
    if written != expected:
        tmp_path.unlink(missing_ok=True)
        raise ApiError(
            400,
            "INVALID_CONFIG",
            f"received {written} bytes but Content-Length was {expected}",
        )
    tmp_path.replace(chunk_path)

    # Recompute received_bytes as the sum of existing chunk file sizes (handles
    # idempotent re-PUTs correctly).
    total_received = sum(p.stat().st_size for p in Path(s.parts_dir).glob("chunk_*.bin"))
    s.received_bytes = total_received
    await db.commit()
    return _to_session_out(s)


@router.post("/uploads/{upload_id}/complete", response_model=AssetOut)
async def complete_upload(
    upload_id: str, db: AsyncSession = Depends(get_db)
) -> AssetOut:
    s = await db.get(dbmod.UploadSession, upload_id)
    if s is None:
        raise ApiError(404, "UPLOAD_SESSION_NOT_FOUND", f"upload {upload_id} not found")
    if s.status != "active":
        raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", f"upload status={s.status}")
    # Recompute from disk rather than trusting the stored counter: parallel
    # chunk PUTs can race their sum-then-commit and persist a short total even
    # though every chunk landed. Disk is the source of truth.
    received = sum(
        p.stat().st_size for p in Path(s.parts_dir).glob("chunk_*.bin")
    )
    if received != s.total_bytes:
        raise ApiError(
            400,
            "UPLOAD_CHUNK_OUT_OF_ORDER",
            f"expected {s.total_bytes} bytes; received {received}",
            total_bytes=s.total_bytes,
            received_bytes=received,
        )

    parts_dir = Path(s.parts_dir)
    parts = sorted(parts_dir.glob("chunk_*.bin"))
    chunks_total = (s.total_bytes + s.chunk_size - 1) // s.chunk_size
    if len(parts) != chunks_total:
        raise ApiError(
            400,
            "UPLOAD_CHUNK_OUT_OF_ORDER",
            f"expected {chunks_total} chunks; found {len(parts)}",
        )

    # Assemble into a temp file first; compute content-addressed id post-hoc.
    ext = Path(s.filename).suffix.lstrip(".").lower() or "mp4"
    tmp_final = settings.data_dir / "uploads" / f".assembling.{s.id}.{ext}"
    tmp_final.parent.mkdir(parents=True, exist_ok=True)
    with tmp_final.open("wb") as out:
        for p in parts:
            with p.open("rb") as src:
                shutil.copyfileobj(src, out, length=64 * 1024)

    # Phase 0 content-addressed id = sha256(head 1 MiB + tail 1 MiB + size)
    try:
        asset = probe(tmp_final)
    except Exception as exc:
        # Undecodable file (HEIC, a corrupt transfer, something that isn't
        # media at all). Bin the assembled temp file rather than leaving GBs
        # of garbage behind, and say something the user can act on.
        tmp_final.unlink(missing_ok=True)
        shutil.rmtree(parts_dir, ignore_errors=True)
        s.status = "aborted"
        s.completed_at = datetime.now(timezone.utc)
        await db.commit()
        suffix = Path(s.filename).suffix.lower().lstrip(".")
        hint = (
            " iPhone HEIC photos aren't supported yet — in Photos choose "
            "File → Export → Export Photo and pick JPEG, or set Settings → "
            "Camera → Formats → Most Compatible."
            if suffix in {"heic", "heif"}
            else ""
        )
        raise ApiError(
            400,
            "UPLOAD_UNSUPPORTED_TYPE",
            f"could not read {s.filename!r} as media.{hint}",
            detail=str(exc)[:200],
        )
    final_path = _final_path(asset.id, s.filename)
    if final_path.exists():
        final_path.unlink()
    tmp_final.replace(final_path)
    # Re-probe with the final path so probe_json references the durable path.
    asset = probe(final_path)

    shutil.rmtree(parts_dir, ignore_errors=True)

    # Avoid duplicate Asset rows if the same content was uploaded before.
    existing = await db.get(dbmod.Asset, asset.id)
    if existing is None:
        a = dbmod.Asset(
            id=asset.id,
            project_id=s.project_id,
            kind="photo" if asset.is_photo else "video",
            path=str(asset.path),
            original_filename=s.filename,
            duration_sec=asset.probe.duration_s,
            width=asset.probe.width,
            height=asset.probe.height,
            fps=asset.probe.fps,
            has_audio=asset.has_audio,
            size_bytes=asset.size_bytes,
            probe_json=json.dumps(asset_to_dict(asset)),
        )
        db.add(a)
    else:
        a = existing

    s.status = "completed"
    s.asset_id = asset.id
    s.completed_at = datetime.now(timezone.utc)
    # If the project has no source_asset_id yet, set it to the first uploaded asset.
    p = await db.get(dbmod.Project, s.project_id)
    if p is not None and p.source_asset_id is None:
        p.source_asset_id = asset.id

    await db.commit()

    return AssetOut(
        id=a.id,
        project_id=a.project_id,
        path=a.path,
        original_filename=a.original_filename,
        duration_sec=a.duration_sec,
        width=a.width,
        height=a.height,
        fps=a.fps,
        has_audio=a.has_audio,
        size_bytes=a.size_bytes,
        created_at=a.created_at,
    )


@router.delete("/uploads/{upload_id}", status_code=204)
async def abort_upload(
    upload_id: str, db: AsyncSession = Depends(get_db)
) -> None:
    s = await db.get(dbmod.UploadSession, upload_id)
    if s is None:
        raise ApiError(404, "UPLOAD_SESSION_NOT_FOUND", f"upload {upload_id} not found")
    if s.status == "completed":
        # Aborting a finished upload must not clobber its status — the asset
        # already exists and the parts dir was consumed by assembly.
        raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", "upload already completed")
    shutil.rmtree(Path(s.parts_dir), ignore_errors=True)
    s.status = "aborted"
    s.completed_at = datetime.now(timezone.utc)
    await db.commit()


async def _aopen_write(path: Path):
    import aiofiles

    return aiofiles.open(path, "wb")
