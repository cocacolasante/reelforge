"""Compose endpoints (POST + GET compose manifest)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.models import ComposeConfig

router = APIRouter(tags=["compose"])


async def _load_reel(db: AsyncSession, reel_id: str) -> dbmod.Reel:
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    return r


@router.post("/reels/{reel_id}/compose", response_model=JobOut)
async def enqueue_compose(
    reel_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    reel = await _load_reel(db, reel_id)
    # Inject reel-level trim offsets into the compose config (API owns these).
    body = dict(body)
    body.setdefault("trim_start_offset_sec", reel.trim_start_offset_sec)
    body.setdefault("trim_end_offset_sec", reel.trim_end_offset_sec)
    config = ComposeConfig(**body)

    job_row = await enqueue_job(
        db,
        arq,
        project_id=reel.project_id,
        kind="compose",
        function_name="compose_reel_job",
        function_args=[reel.asset_id, reel_id, config.model_dump()],
        config=config,
        asset_id=reel.asset_id,
        reel_id=reel_id,
        conflict_filter=(dbmod.Job.kind == "compose") & (dbmod.Job.reel_id == reel_id),
    )
    # Link reel → compose job for the UI
    reel.compose_job_id = job_row.id
    await db.commit()
    return JobOut(**await job_with_live_progress(db, None, job_row.id))


@router.get("/reels/{reel_id}/compose")
async def get_compose_manifest(
    reel_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    reel = await _load_reel(db, reel_id)
    wd = working_dir_for(reel.asset_id)
    compose_json = wd / "reels" / reel_id / "compose.json"
    if not compose_json.exists():
        raise ApiError(
            404, "MEZZANINE_NOT_READY", f"compose.json missing for reel {reel_id}"
        )
    return json.loads(compose_json.read_text())
