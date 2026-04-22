"""Cost estimate + actual spend endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.errors import ApiError
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    SelectionConfig,
)
from reelforge_core.pricing import (
    PRICING_AS_OF,
    estimate_ranking_cost,
    estimate_semantics_cost,
)
from reelforge_core.reels.candidates import generate_candidates
from reelforge_core.usage import aggregate_usage

router = APIRouter(tags=["cost"])


@router.post("/assets/{asset_id}/analyze/estimate")
async def estimate_analyze_cost(
    asset_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
) -> dict:
    asset = await db.get(dbmod.Asset, asset_id)
    if asset is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    cfg = AnalysisConfig(**body)
    # Without a prior probe, estimate scene count from duration using a
    # conservative ~12-second average scene length.
    scene_count = max(1, int(asset.duration_sec / 12))
    breakdown = estimate_semantics_cost(
        scene_count=scene_count, model=cfg.semantics_model
    )
    return {
        "pricing_as_of": PRICING_AS_OF,
        "estimated_cost_usd": breakdown["estimated_cost_usd"],
        "scene_count_estimate": scene_count,
        "breakdown": [breakdown],
        "note": "Estimate assumes a fresh analyze; cache hits reduce actual cost.",
    }


@router.post("/assets/{asset_id}/select/estimate")
async def estimate_select_cost(
    asset_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
) -> dict:
    asset = await db.get(dbmod.Asset, asset_id)
    if asset is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    # Need an analysis to count candidates.
    wd = working_dir_for(asset_id)
    analysis_path = wd / "analysis.json"
    if not analysis_path.exists():
        raise ApiError(
            409,
            "ANALYSIS_NOT_READY",
            "Run analyze before estimating select cost.",
        )
    try:
        analysis = AnalysisReport.model_validate_json(analysis_path.read_text())
    except Exception as exc:
        raise ApiError(500, "INTERNAL_ERROR", f"failed to read analysis.json: {exc}")
    cfg = SelectionConfig(**body)
    candidates = generate_candidates(analysis, cfg)
    breakdown = estimate_ranking_cost(
        candidate_count=len(candidates), model=cfg.ranking_model
    )
    return {
        "pricing_as_of": PRICING_AS_OF,
        "estimated_cost_usd": breakdown["estimated_cost_usd"],
        "candidate_count": len(candidates),
        "breakdown": [breakdown],
    }


@router.get("/projects/{project_id}/usage")
async def project_usage(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    p = await db.get(dbmod.Project, project_id)
    if p is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")
    agg = await aggregate_usage(project_id=project_id)
    return {"project_id": project_id, "pricing_as_of": PRICING_AS_OF, **agg}


@router.get("/usage")
async def global_usage() -> dict:
    agg = await aggregate_usage()
    return {"pricing_as_of": PRICING_AS_OF, **agg}
