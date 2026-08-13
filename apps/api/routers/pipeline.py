"""Analyze + select endpoints. Enqueue-only — heavy work runs in the worker."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut, ReelList, ReelOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from apps.api.settings import settings
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.ingest import asset_to_dict, probe
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    ReelSelection,
    SelectionConfig,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["pipeline"])


async def _ensure_asset(db: AsyncSession, asset_id: str) -> dbmod.Asset:
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    return a


def _write_probe_json(asset_row: dbmod.Asset) -> None:
    """Ensure /data/working/{asset_id}/probe.json exists so the worker can
    reconstitute the MediaAsset at job time."""
    wd = working_dir_for(asset_row.id)
    probe_path = wd / "probe.json"
    if probe_path.exists():
        return
    # Re-run probe against the asset path on disk.
    asset = probe(Path(asset_row.path))
    write_json_atomic(probe_path, asset_to_dict(asset))


@router.post("/assets/{asset_id}/analyze", response_model=JobOut)
async def analyze_asset_endpoint(
    asset_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    row = await _ensure_asset(db, asset_id)
    config = AnalysisConfig(**body)
    _write_probe_json(row)

    job_row = await enqueue_job(
        db,
        arq,
        project_id=row.project_id,
        kind="analyze",
        function_name="analyze_asset",
        function_args=[asset_id, config.model_dump()],
        config=config,
        asset_id=asset_id,
        conflict_filter=(dbmod.Job.kind == "analyze") & (dbmod.Job.asset_id == asset_id),
    )
    return JobOut(**await job_with_live_progress(db, None, job_row.id))


@router.get("/assets/{asset_id}/analysis")
async def get_analysis(asset_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    row = await _ensure_asset(db, asset_id)
    # Read-only endpoint: build the path without working_dir_for(), which
    # mkdirs working/{asset_id}/thumbs as a side effect.
    from apps.api.settings import settings

    analysis_path = settings.data_dir / "working" / asset_id / "analysis.json"
    if not analysis_path.exists():
        raise ApiError(
            404,
            "ANALYSIS_NOT_READY",
            f"analysis.json not yet produced for asset {asset_id}",
        )
    try:
        return json.loads(analysis_path.read_text())
    except Exception as exc:
        raise ApiError(500, "INTERNAL_ERROR", f"failed to read analysis.json: {exc}")


@router.post("/assets/{asset_id}/select", response_model=JobOut)
async def select_reels_endpoint(
    asset_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    row = await _ensure_asset(db, asset_id)
    # Require analysis.json to exist before enqueuing.
    wd = working_dir_for(asset_id)
    if not (wd / "analysis.json").exists():
        raise ApiError(
            409,
            "ANALYSIS_NOT_READY",
            "Run analyze before select — analysis.json is missing.",
        )
    config = SelectionConfig(**body)

    job_row = await enqueue_job(
        db,
        arq,
        project_id=row.project_id,
        kind="select",
        function_name="select_reels_job",
        function_args=[asset_id, config.model_dump()],
        config=config,
        asset_id=asset_id,
        conflict_filter=(dbmod.Job.kind == "select") & (dbmod.Job.asset_id == asset_id),
    )
    return JobOut(**await job_with_live_progress(db, None, job_row.id))


def _hydrate_reels_from_disk(
    db_session: AsyncSession, asset: dbmod.Asset
) -> list[ReelOut]:
    """Load reels.json from disk and materialise ReelOut records. Does not
    touch the DB — the caller commits if it wants to cache the rows."""
    wd = working_dir_for(asset.id)
    reels_path = wd / "reels.json"
    if not reels_path.exists():
        raise ApiError(
            404,
            "SELECTION_NOT_READY",
            "Run select before listing reels — reels.json is missing.",
        )
    selection = ReelSelection.model_validate_json(reels_path.read_text())
    out: list[ReelOut] = []
    for r in selection.reels:
        mezz = wd / "reels" / r.candidate_id / "mezzanine.mp4"
        out.append(
            ReelOut(
                id=r.candidate_id,
                project_id=asset.project_id,
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
                scene_indices=r.scene_indices,
                scores=r.scores.model_dump(),
                mezzanine_ready=mezz.exists(),
            )
        )
    return out


@router.get("/assets/{asset_id}/reels", response_model=ReelList)
async def list_reels_for_asset(
    asset_id: str, db: AsyncSession = Depends(get_db)
) -> ReelList:
    asset = await _ensure_asset(db, asset_id)
    reels = _hydrate_reels_from_disk(db, asset)

    # Upsert DB rows so downstream endpoints (compose, export) can resolve by id.
    for r in reels:
        existing = await db.get(dbmod.Reel, r.id)
        if existing is None:
            db.add(
                dbmod.Reel(
                    id=r.id,
                    project_id=r.project_id,
                    asset_id=r.asset_id,
                    rank=r.rank,
                    title=r.title,
                    hook=r.hook,
                    justification=r.justification,
                    start_sec=r.start_sec,
                    end_sec=r.end_sec,
                    duration_sec=r.duration_sec,
                    overall_score=r.overall_score,
                    suggested_mood=r.suggested_mood,
                    scene_indices_json=json.dumps(r.scene_indices),
                    scores_json=json.dumps(r.scores),
                    mezzanine_path=(
                        str(
                            working_dir_for(asset_id)
                            / "reels"
                            / r.id
                            / "mezzanine.mp4"
                        )
                        if r.mezzanine_ready
                        else None
                    ),
                )
            )
    await db.commit()
    return ReelList(reels=reels)
