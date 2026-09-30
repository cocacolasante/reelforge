"""Cut requests from an agent: one call, one job to poll.

The dashboard drives analyze -> select -> compose -> export as four jobs
because a person is watching each step and choosing what comes next. An
agent is not: it asks for clips and comes back later. These routes do the
setup that needs the database (validating the project, picking the primary
clip, creating the mix row) and hand the sequence to `agent_cut_job`.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from reelforge_core.analysis.pipeline import working_dir_for

router = APIRouter(tags=["agent"])
log = logging.getLogger(__name__)

MAX_CLIPS = 10
LONG_MIN_SEC = 60.0
LONG_MAX_SEC = 1800.0


class CutRequest(BaseModel):
    project_id: str
    # "reels" = several short vertical clips; "long" = one longer video mixed
    # from every clip in the project.
    mode: str = Field(default="reels", pattern="^(reels|long)$")
    count: int | None = Field(default=None, ge=1, le=MAX_CLIPS)
    min_sec: float | None = Field(default=None, ge=3, le=600)
    max_sec: float | None = Field(default=None, ge=5, le=1800)
    prompt: str | None = Field(default=None, max_length=500)
    # Any of links | folder | email. Empty means the configured default.
    delivery: list[str] | None = None
    target_duration_sec: float | None = Field(
        default=None, ge=LONG_MIN_SEC, le=LONG_MAX_SEC
    )


async def _video_assets(db: AsyncSession, project_id: str) -> list[dbmod.Asset]:
    rows = (
        (
            await db.execute(
                select(dbmod.Asset).where(
                    dbmod.Asset.project_id == project_id,
                    dbmod.Asset.kind == "video",
                )
            )
        )
        .scalars()
        .all()
    )
    # A row whose file has gone is worse than no row: the worker would fail
    # on it minutes into a run.
    return [a for a in rows if Path(a.path).exists()]


@router.post("/agent/cuts", response_model=JobOut)
async def create_cut(
    body: CutRequest,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    project = await db.get(dbmod.Project, body.project_id)
    if project is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {body.project_id} not found")

    assets = await _video_assets(db, body.project_id)
    if not assets:
        raise ApiError(
            409,
            "NO_FOOTAGE",
            f"{project.name!r} has no video in it yet — send footage first.",
        )

    options: dict = {"project_name": project.name}
    if body.prompt:
        options["prompt"] = body.prompt
    if body.delivery:
        unknown = [c for c in body.delivery if c not in ("links", "folder", "email")]
        if unknown:
            raise ApiError(
                422, "INVALID_CONFIG", f"unknown delivery: {', '.join(unknown)}"
            )
        options["delivery"] = body.delivery
    mix_id: str | None = None
    primary_id: str | None = None

    if body.mode == "long":
        if len(assets) < 2:
            raise ApiError(
                409,
                "NOT_ENOUGH_CLIPS",
                "A long video is mixed from several clips; this project has one. "
                "Ask for reels instead.",
            )
        # Primary = the longest clip; its working dir hosts the render. It
        # needs a probe.json, which every uploaded asset already has.
        primary = max(assets, key=lambda a: a.duration_sec or 0.0)
        if not (working_dir_for(primary.id) / "probe.json").exists():
            raise ApiError(
                409, "ASSET_NOT_READY", f"clip {primary.id[:12]} has no probe.json"
            )
        primary_id = primary.id
        mix_id = f"mix-{uuid.uuid4().hex[:12]}"
        target = body.target_duration_sec or 300.0
        options["target_duration_sec"] = target
        db.add(
            dbmod.Reel(
                id=mix_id,
                project_id=project.id,
                asset_id=primary.id,
                rank=0,
                title="Long video (working…)",
                hook="",
                justification="agent request",
                start_sec=0.0,
                end_sec=target,
                duration_sec=target,
                overall_score=0.0,
                suggested_mood="neutral",
                scene_indices_json="[]",
                scores_json=json.dumps(
                    {
                        "narrative_coherence": 0,
                        "hook_strength": 0,
                        "emotional_payoff": 0,
                        "standalone_clarity": 0,
                    }
                ),
            )
        )
        await db.commit()
    else:
        options["top_k"] = body.count or 3
        if body.min_sec is not None:
            options["min_sec"] = body.min_sec
        if body.max_sec is not None:
            options["max_sec"] = body.max_sec

    sources = [(a.id, a.path, a.original_filename or Path(a.path).name) for a in assets]
    job_row = await enqueue_job(
        db,
        arq,
        project_id=project.id,
        kind="agent_cut",
        function_name="agent_cut_job",
        function_args=[project.id, body.mode, sources, options, mix_id, primary_id],
        config=body,
        asset_id=primary_id,
        reel_id=mix_id,
        # Cutting costs tokens and tens of minutes of CPU. One run per
        # project at a time, so a confused agent can't queue ten.
        conflict_filter=(dbmod.Job.kind == "agent_cut")
        & (dbmod.Job.project_id == project.id),
    )
    log.info(
        "agent cut queued: project=%s mode=%s clips=%d", project.id, body.mode, len(assets)
    )
    return JobOut(**await job_with_live_progress(db, None, job_row.id))
