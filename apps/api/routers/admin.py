"""Admin + operations endpoints: disk usage, cleanup, batch-compose."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from arq.connections import ArqRedis
from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from apps.api.settings import settings
from reelforge_core import cache as file_cache
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.models import ComposeConfig

log = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                continue
    return total


@router.get("/disk_usage")
async def global_disk_usage() -> dict[str, Any]:
    data = settings.data_dir
    working = data / "working"
    outputs = data / "outputs"
    uploads = data / "uploads"
    cache = data / "cache"
    return {
        "data_dir": str(data),
        "working_bytes": _dir_size(working),
        "outputs_bytes": _dir_size(outputs),
        "uploads_bytes": _dir_size(uploads),
        "cache_bytes": _dir_size(cache),
        "cache_breakdown": {
            "clip": file_cache.cache_size("clip"),
            "music": file_cache.cache_size("music"),
            "caption_preview": file_cache.cache_size("caption_preview"),
        },
    }


@router.get("/projects/{project_id}/disk_usage")
async def project_disk_usage(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")

    data = settings.data_dir
    # Sum disk usage per asset belonging to this project.
    rows = (
        await db.execute(
            select(dbmod.Asset.id, dbmod.Asset.path).where(
                dbmod.Asset.project_id == project_id
            )
        )
    ).all()
    breakdown: list[dict[str, Any]] = []
    total_working = 0
    total_outputs = 0
    total_uploads = 0
    for row in rows:
        asset_id = row[0]
        upload_path = Path(row[1]) if row[1] else None
        w = _dir_size(data / "working" / asset_id)
        o = _dir_size(data / "outputs" / asset_id)
        u = upload_path.stat().st_size if upload_path and upload_path.exists() else 0
        breakdown.append(
            {
                "asset_id": asset_id,
                "working_bytes": w,
                "outputs_bytes": o,
                "uploads_bytes": u,
                "total_bytes": w + o + u,
            }
        )
        total_working += w
        total_outputs += o
        total_uploads += u
    return {
        "project_id": project_id,
        "working_bytes": total_working,
        "outputs_bytes": total_outputs,
        "uploads_bytes": total_uploads,
        "total_bytes": total_working + total_outputs + total_uploads,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

CleanupMode = Literal["working", "outputs", "all", "safe"]


class CleanupRequest(BaseModel):
    mode: CleanupMode = "safe"


def _cleanup_paths_for_mode(
    mode: CleanupMode, asset_id: str, upload_path: Path | None
) -> list[Path]:
    data = settings.data_dir
    working = data / "working" / asset_id
    outputs = data / "outputs" / asset_id
    safe_targets = [working / "tmp"]
    # All per-reel tmp dirs under working/{asset_id}/reels/*/tmp are also safe
    for p in (working / "reels").glob("*/tmp") if working.exists() else []:
        safe_targets.append(p)
    if mode == "safe":
        return safe_targets
    if mode == "working":
        return [working]
    if mode == "outputs":
        return [outputs]
    if mode == "all":
        out = [working, outputs]
        if upload_path is not None:
            out.append(upload_path)
        return out
    return []


@router.post("/projects/{project_id}/cleanup")
async def project_cleanup(
    project_id: str,
    body: CleanupRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    project = await db.get(dbmod.Project, project_id)
    if project is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")

    assets = (
        await db.execute(
            select(dbmod.Asset.id, dbmod.Asset.path).where(
                dbmod.Asset.project_id == project_id
            )
        )
    ).all()
    freed = 0
    removed_paths: list[str] = []
    for row in assets:
        asset_id = row[0]
        upload_path = Path(row[1]) if row[1] else None
        for p in _cleanup_paths_for_mode(body.mode, asset_id, upload_path):
            if not p.exists():
                continue
            size = _dir_size(p) if p.is_dir() else p.stat().st_size
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except OSError:
                    continue
            freed += size
            removed_paths.append(str(p))
    return {
        "project_id": project_id,
        "mode": body.mode,
        "bytes_freed": freed,
        "removed": removed_paths,
    }


@router.post("/cache/purge")
async def purge_cache(kind: str = Query(...)) -> dict[str, int]:
    if kind not in {"clip", "music", "caption_preview"}:
        raise ApiError(400, "INVALID_CONFIG", f"unknown cache kind {kind!r}")
    freed = file_cache.purge_kind(kind)
    return {"bytes_freed": freed}


# ---------------------------------------------------------------------------
# Compose presets (scoped CRUD)
# ---------------------------------------------------------------------------


class ComposePresetIn(BaseModel):
    name: str
    scope: str = "global"  # "global" or "project:{id}"
    config: dict[str, Any]


class ComposePresetOut(BaseModel):
    id: str
    name: str
    scope: str
    config: dict[str, Any]
    created_at: datetime


# Presets are stored in a tiny KV table. Created lazily to avoid another migration.
async def _ensure_presets_table(db: AsyncSession) -> None:
    from sqlalchemy import text

    await db.execute(
        text(
            """CREATE TABLE IF NOT EXISTS compose_presets (
                 id TEXT PRIMARY KEY,
                 name TEXT NOT NULL,
                 scope TEXT NOT NULL,
                 config_json TEXT NOT NULL,
                 created_at TEXT NOT NULL
               )"""
        )
    )


@router.post("/compose_presets", response_model=ComposePresetOut, status_code=201)
async def create_preset(
    body: ComposePresetIn, db: AsyncSession = Depends(get_db)
) -> ComposePresetOut:
    import json
    import uuid

    from sqlalchemy import text

    # Validate the config round-trips into ComposeConfig.
    try:
        ComposeConfig(**body.config)
    except Exception as exc:
        raise ApiError(400, "INVALID_CONFIG", str(exc))

    await _ensure_presets_table(db)
    pid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        text(
            "INSERT INTO compose_presets (id, name, scope, config_json, created_at) "
            "VALUES (:id, :name, :scope, :config, :created_at)"
        ),
        {
            "id": pid,
            "name": body.name,
            "scope": body.scope,
            "config": json.dumps(body.config),
            "created_at": now,
        },
    )
    await db.commit()
    return ComposePresetOut(
        id=pid,
        name=body.name,
        scope=body.scope,
        config=body.config,
        created_at=datetime.fromisoformat(now),
    )


@router.get("/compose_presets", response_model=list[ComposePresetOut])
async def list_presets(
    scope: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[ComposePresetOut]:
    import json

    from sqlalchemy import text

    await _ensure_presets_table(db)
    if scope:
        rows = (
            await db.execute(
                text(
                    "SELECT id, name, scope, config_json, created_at FROM compose_presets "
                    "WHERE scope=:scope OR scope='global' ORDER BY created_at DESC"
                ),
                {"scope": scope},
            )
        ).mappings().all()
    else:
        rows = (
            await db.execute(
                text(
                    "SELECT id, name, scope, config_json, created_at FROM compose_presets "
                    "ORDER BY created_at DESC"
                )
            )
        ).mappings().all()
    return [
        ComposePresetOut(
            id=r["id"],
            name=r["name"],
            scope=r["scope"],
            config=json.loads(r["config_json"]),
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]


@router.delete("/compose_presets/{preset_id}", status_code=204)
async def delete_preset(
    preset_id: str, db: AsyncSession = Depends(get_db)
) -> None:
    from sqlalchemy import text

    await _ensure_presets_table(db)
    await db.execute(
        text("DELETE FROM compose_presets WHERE id=:id"), {"id": preset_id}
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Batch compose
# ---------------------------------------------------------------------------


class BatchComposeRequest(BaseModel):
    reel_ids: list[str]
    config: dict[str, Any] = {}


@router.post("/assets/{asset_id}/compose_batch")
async def batch_compose(
    asset_id: str,
    body: BatchComposeRequest,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> dict[str, Any]:
    asset = await db.get(dbmod.Asset, asset_id)
    if asset is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    try:
        cfg = ComposeConfig(**(body.config or {}))
    except Exception as exc:
        raise ApiError(400, "INVALID_CONFIG", str(exc))

    job_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    for reel_id in body.reel_ids:
        reel = await db.get(dbmod.Reel, reel_id)
        if reel is None or reel.asset_id != asset_id:
            skipped.append({"reel_id": reel_id, "reason": "REEL_NOT_FOUND"})
            continue
        try:
            job = await enqueue_job(
                db,
                arq,
                project_id=reel.project_id,
                kind="compose",
                function_name="compose_reel_job",
                function_args=[asset_id, reel_id, cfg.model_dump()],
                config=cfg,
                asset_id=asset_id,
                reel_id=reel_id,
                conflict_filter=(
                    (dbmod.Job.kind == "compose") & (dbmod.Job.reel_id == reel_id)
                ),
            )
            job_ids.append(job.id)
        except ApiError as exc:
            skipped.append({"reel_id": reel_id, "reason": exc.code})
    return {"job_ids": job_ids, "skipped": skipped}
