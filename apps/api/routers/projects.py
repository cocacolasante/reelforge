"""Project CRUD + asset listing."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import (
    AssetList,
    AssetOut,
    ProjectCreate,
    ProjectList,
    ProjectOut,
)
from apps.api.schemas.errors import ApiError

router = APIRouter(tags=["projects"])
log = logging.getLogger(__name__)


def _to_project_out(p: dbmod.Project) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        created_at=p.created_at,
        source_asset_id=p.source_asset_id,
    )


def _analysis_ready(asset_id: str) -> bool:
    # Deliberately NOT working_dir_for(): that helper mkdirs as a side effect.
    from apps.api.settings import settings

    return (settings.data_dir / "working" / asset_id / "analysis.json").exists()


def _to_asset_out(a: dbmod.Asset) -> AssetOut:
    return AssetOut(
        id=a.id,
        project_id=a.project_id,
        kind=a.kind,
        path=a.path,
        original_filename=a.original_filename,
        duration_sec=a.duration_sec,
        width=a.width,
        height=a.height,
        fps=a.fps,
        has_audio=a.has_audio,
        size_bytes=a.size_bytes,
        created_at=a.created_at,
        analysis_ready=_analysis_ready(a.id),
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
    # Collect filesystem targets BEFORE deleting the rows that point at them.
    assets = (
        await db.execute(
            select(dbmod.Asset).where(dbmod.Asset.project_id == project_id)
        )
    ).scalars().all()
    sessions = (
        await db.execute(
            select(dbmod.UploadSession).where(
                dbmod.UploadSession.project_id == project_id
            )
        )
    ).scalars().all()
    from apps.api.settings import settings

    paths_to_remove: list[Path] = []
    for a in assets:
        paths_to_remove.append(settings.data_dir / "working" / a.id)
        paths_to_remove.append(settings.data_dir / "outputs" / a.id)
        paths_to_remove.append(Path(a.path))
    for s in sessions:
        if s.parts_dir:
            paths_to_remove.append(Path(s.parts_dir))

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

    # Remove the project's files off the event loop. Runs after the commit so
    # a failed DB delete never half-removes data; multi-GB uploads make this
    # worth doing properly rather than leaking forever.
    def _rm_all(paths: list[Path]) -> None:
        for target in paths:
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort cleanup
                log.warning("could not remove %s during project delete", target)

    await asyncio.to_thread(_rm_all, paths_to_remove)


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


@router.delete("/assets/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> None:
    """Remove a source clip: stop its jobs, drop its rows, delete its files.

    Deleting mid-analysis is the common case (a clip turns out to be too big
    or simply wrong), so in-flight jobs are aborted first — otherwise FFmpeg
    keeps burning CPU on a file that no longer exists.
    """
    asset = await db.get(dbmod.Asset, asset_id)
    if asset is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")

    # 1. Stop anything running on this asset.
    live_jobs = (
        await db.execute(
            select(dbmod.Job).where(
                dbmod.Job.asset_id == asset_id,
                dbmod.Job.status.in_(("queued", "running")),
            )
        )
    ).scalars().all()
    for job in live_jobs:
        try:
            from arq.jobs import Job as ArqJob

            await asyncio.wait_for(
                ArqJob(job.id, redis=arq).abort(timeout=0.1), timeout=2.0
            )
        except Exception:
            # Abort is best-effort: the worker may not be running, or may not
            # have abort enabled. The DB row below still reflects reality.
            log.info("could not abort job %s for deleted asset", job.id)
        job.status = "failed"
        job.error_message = "asset deleted"
        job.finished_at = datetime.now(timezone.utc)

    # 2. Collect file targets before the rows that name them are gone.
    from apps.api.settings import settings

    reel_ids = [
        r
        for (r,) in (
            await db.execute(
                select(dbmod.Reel.id).where(dbmod.Reel.asset_id == asset_id)
            )
        ).all()
    ]
    paths_to_remove: list[Path] = [
        settings.data_dir / "working" / asset_id,
        settings.data_dir / "outputs" / asset_id,
        Path(asset.path),
    ]
    sessions = (
        await db.execute(
            select(dbmod.UploadSession).where(
                dbmod.UploadSession.asset_id == asset_id
            )
        )
    ).scalars().all()
    for s in sessions:
        if s.parts_dir:
            paths_to_remove.append(Path(s.parts_dir))

    # 3. Cascade the rows (children first — SQLite FK cascade isn't reliable
    # across SQLModel versions without explicit DDL).
    if reel_ids:
        await db.execute(
            delete(dbmod.Publication).where(dbmod.Publication.reel_id.in_(reel_ids))
        )
        await db.execute(
            delete(dbmod.Export).where(dbmod.Export.reel_id.in_(reel_ids))
        )
    await db.execute(delete(dbmod.Reel).where(dbmod.Reel.asset_id == asset_id))
    await db.execute(delete(dbmod.Job).where(dbmod.Job.asset_id == asset_id))
    await db.execute(
        delete(dbmod.UploadSession).where(dbmod.UploadSession.asset_id == asset_id)
    )
    # A project points at its first asset; clear the pointer if it was this one.
    project = await db.get(dbmod.Project, asset.project_id)
    if project is not None and project.source_asset_id == asset_id:
        remaining = (
            await db.execute(
                select(dbmod.Asset.id)
                .where(
                    dbmod.Asset.project_id == asset.project_id,
                    dbmod.Asset.id != asset_id,
                )
                .order_by(dbmod.Asset.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        project.source_asset_id = remaining
    await db.delete(asset)
    await db.commit()

    # 4. Remove files off the event loop, after the commit.
    def _rm_all(paths: list[Path]) -> None:
        for target in paths:
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort cleanup
                log.warning("could not remove %s during asset delete", target)

    await asyncio.to_thread(_rm_all, paths_to_remove)
    log.info("deleted asset %s (%s)", asset_id, asset.original_filename)


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
            log.warning(
                "skipping unparseable reels.json for asset %s", asset.id, exc_info=True
            )
            continue
        for r in sel.reels:
            mezz = wd / "reels" / r.candidate_id / "mezzanine.mp4"
            # Upsert the Reel row: everything downstream (GET /reels/{id},
            # compose, export, preview) resolves reels through the DB, and
            # this aggregation is the only reel-listing path the UI uses —
            # without the upsert every reel link 404s. Re-selects refresh
            # rank/title in place; stale rows for dropped candidates are kept
            # (exports may reference them) but never surface here.
            existing = await db.get(dbmod.Reel, r.candidate_id)
            mezz_path = str(mezz) if mezz.exists() else None
            if existing is None:
                db.add(
                    dbmod.Reel(
                        id=r.candidate_id,
                        project_id=project_id,
                        asset_id=asset.id,
                        rank=r.rank,
                        title=r.title,
                        hook=r.hook,
                        justification=r.justification,
                        start_sec=r.start_sec,
                        end_sec=r.end_sec,
                        duration_sec=r.duration_sec,
                        overall_score=r.overall,
                        suggested_mood=r.suggested_mood,
                        scene_indices_json=_json.dumps(r.scene_indices),
                        scores_json=_json.dumps(r.scores.model_dump()),
                        prompt_relevance=r.prompt_relevance,
                        source=r.source,
                        opening_description=r.opening_description,
                        mezzanine_path=mezz_path,
                    )
                )
            else:
                existing.rank = r.rank
                existing.title = r.title
                existing.hook = r.hook
                existing.justification = r.justification
                existing.overall_score = r.overall
                existing.suggested_mood = r.suggested_mood
                # Unconditional: a promptless re-select clears stale values.
                existing.prompt_relevance = r.prompt_relevance
                existing.source = r.source
                existing.opening_description = r.opening_description
                # v2: candidate_id hashes the bounds, but refinement (CP7) can
                # move bounds under a stable id — always refresh geometry.
                existing.start_sec = r.start_sec
                existing.end_sec = r.end_sec
                existing.duration_sec = r.duration_sec
                existing.scene_indices_json = _json.dumps(r.scene_indices)
                if mezz_path:
                    existing.mezzanine_path = mezz_path
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
                    "prompt_relevance": r.prompt_relevance,
                    "source": r.source,
                    "opening_description": r.opening_description,
                }
            )
    # Re-rank merged set by overall score so the UI shows the best content first
    # regardless of which source clip it came from.
    await db.commit()
    merged.sort(key=lambda r: r["overall_score"], reverse=True)
    for i, r in enumerate(merged, 1):
        r["project_rank"] = i
    return {"reels": merged, "asset_count": len(assets)}
