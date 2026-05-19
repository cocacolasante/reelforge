"""Project CRUD + asset listing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.common import (
    AssetList,
    AssetOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
)
from apps.api.schemas.errors import ApiError

router = APIRouter(tags=["projects"])


def _to_project_out(p: dbmod.Project) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        created_at=p.created_at,
        source_asset_id=p.source_asset_id,
    )


def _to_asset_out(a: dbmod.Asset) -> AssetOut:
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


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate, db: AsyncSession = Depends(get_db)
) -> ProjectOut:
    p = dbmod.Project(name=body.name.strip() or "Untitled")
    db.add(p)
    await db.commit()
    return _to_project_out(p)


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ProjectList:
    total = (
        await db.execute(select(func.count()).select_from(dbmod.Project))
    ).scalar_one()
    rows = (
        await db.execute(
            select(dbmod.Project)
            .order_by(dbmod.Project.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return ProjectList(
        projects=[_to_project_out(p) for p in rows],
        total=int(total),
    )


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)) -> ProjectOut:
    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")
    return _to_project_out(p)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> None:
    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")
    # Cascade: delete dependent rows first (SQLite FK cascade isn't reliable
    # across SQLModel versions without ON DELETE CASCADE DDL).
    await db.execute(
        delete(dbmod.Export).where(
            dbmod.Export.reel_id.in_(
                select(dbmod.Reel.id).where(dbmod.Reel.project_id == project_id)
            )
        )
    )
    await db.execute(delete(dbmod.Reel).where(dbmod.Reel.project_id == project_id))
    await db.execute(delete(dbmod.Job).where(dbmod.Job.project_id == project_id))
    await db.execute(
        delete(dbmod.UploadSession).where(dbmod.UploadSession.project_id == project_id)
    )
    await db.execute(delete(dbmod.Asset).where(dbmod.Asset.project_id == project_id))
    await db.delete(p)
    await db.commit()


@router.get(
    "/projects/{project_id}/assets", response_model=AssetList
)
async def list_assets(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> AssetList:
    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")
    rows = (
        await db.execute(
            select(dbmod.Asset)
            .where(dbmod.Asset.project_id == project_id)
            .order_by(dbmod.Asset.created_at.desc())
        )
    ).scalars().all()
    return AssetList(assets=[_to_asset_out(a) for a in rows])


@router.get("/assets/{asset_id}", response_model=AssetOut)
async def get_asset(asset_id: str, db: AsyncSession = Depends(get_db)) -> AssetOut:
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    return _to_asset_out(a)


@router.get("/projects/{project_id}/reels")
async def list_project_reels(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Aggregated reels across every asset in the project, sorted by score."""
    import json as _json

    from reelforge_core.analysis.pipeline import working_dir_for
    from reelforge_core.models import ReelSelection

    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")

    assets = (
        await db.execute(
            select(dbmod.Asset).where(dbmod.Asset.project_id == project_id)
        )
    ).scalars().all()

    merged: list[dict] = []
    for asset in assets:
        wd = working_dir_for(asset.id)
        reels_path = wd / "reels.json"
        if not reels_path.exists():
            continue
        try:
            sel = ReelSelection.model_validate_json(reels_path.read_text())
        except Exception:
            continue
        for r in sel.reels:
            mezz = wd / "reels" / r.candidate_id / "mezzanine.mp4"
            merged.append(
                {
                    "id": r.candidate_id,
                    "project_id": project_id,
                    "asset_id": asset.id,
                    "asset_filename": asset.original_filename,
                    "rank": r.rank,  # rank within its own asset
                    "title": r.title,
                    "hook": r.hook,
                    "justification": r.justification,
                    "start_sec": r.start_sec,
                    "end_sec": r.end_sec,
                    "duration_sec": r.duration_sec,
                    "overall_score": r.overall,
                    "suggested_mood": r.suggested_mood,
                    "scene_indices": r.scene_indices,
                    "scores": r.scores.model_dump(),
                    "mezzanine_ready": mezz.exists(),
                }
            )
    # Re-rank merged set by overall score so the UI shows the best content first
    # regardless of which source clip it came from.
    merged.sort(key=lambda r: r["overall_score"], reverse=True)
    for i, r in enumerate(merged, 1):
        r["project_rank"] = i
    return {"reels": merged, "asset_count": len(assets)}
