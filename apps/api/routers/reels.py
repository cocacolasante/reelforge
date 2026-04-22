"""Per-reel detail endpoint + trim PATCH + compose-state sync with disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.common import ReelOut
from apps.api.schemas.errors import ApiError
from reelforge_core.analysis.pipeline import working_dir_for

router = APIRouter(tags=["reels"])

MAX_TRIM_OFFSET = 2.0
MIN_REEL_DURATION_AFTER_TRIM = 25.0


class TrimPatch(BaseModel):
    trim_start_offset_sec: Optional[float] = Field(
        default=None, ge=-MAX_TRIM_OFFSET, le=MAX_TRIM_OFFSET
    )
    trim_end_offset_sec: Optional[float] = Field(
        default=None, ge=-MAX_TRIM_OFFSET, le=MAX_TRIM_OFFSET
    )


def _to_reel_out(r: dbmod.Reel, mezz_ready: bool) -> ReelOut:
    return ReelOut(
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
        scene_indices=json.loads(r.scene_indices_json),
        scores=json.loads(r.scores_json),
        mezzanine_ready=mezz_ready,
    )


@router.get("/reels/{reel_id}", response_model=ReelOut)
async def get_reel(reel_id: str, db: AsyncSession = Depends(get_db)) -> ReelOut:
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    mezz = working_dir_for(r.asset_id) / "reels" / reel_id / "mezzanine.mp4"
    if mezz.exists() and r.mezzanine_path != str(mezz):
        r.mezzanine_path = str(mezz)
        await db.commit()
    return _to_reel_out(r, mezz.exists())


@router.patch("/reels/{reel_id}/trim", response_model=ReelOut)
async def patch_trim(
    reel_id: str,
    body: TrimPatch = Body(...),
    db: AsyncSession = Depends(get_db),
) -> ReelOut:
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")

    new_start_offset = (
        body.trim_start_offset_sec
        if body.trim_start_offset_sec is not None
        else r.trim_start_offset_sec
    )
    new_end_offset = (
        body.trim_end_offset_sec
        if body.trim_end_offset_sec is not None
        else r.trim_end_offset_sec
    )

    effective_duration = (r.end_sec - new_end_offset) - (r.start_sec + new_start_offset)
    if effective_duration < MIN_REEL_DURATION_AFTER_TRIM:
        raise ApiError(
            400,
            "INVALID_CONFIG",
            f"trim would shrink reel to {effective_duration:.2f}s; "
            f"minimum {MIN_REEL_DURATION_AFTER_TRIM}s required",
            effective_duration_sec=effective_duration,
        )

    r.trim_start_offset_sec = new_start_offset
    r.trim_end_offset_sec = new_end_offset
    # Any cached mezzanine is now invalid.
    r.mezzanine_path = None
    mezz_path = working_dir_for(r.asset_id) / "reels" / reel_id / "mezzanine.mp4"
    try:
        if mezz_path.exists():
            mezz_path.unlink()
    except OSError:
        pass

    await db.commit()
    return _to_reel_out(r, False)
