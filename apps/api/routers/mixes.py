"""AI Mix: one reel meshing the best moments from EVERY clip in a project.

The mix is stored as a Reel row with synthetic id `mix-{hex}` (the id prefix
is the discriminator — `child_reel_ids_json` stays NULL so mixes don't show
up in the montage list). The worker fills in edit_json/title/mood after
sequencing and renders inline; the mezzanine lands under the PRIMARY asset's
working dir (`working/{primary}/reels/{mix_id}/`) so preview, export,
publish, and the timeline editor all work unchanged.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from reelforge_core.analysis.pipeline import working_dir_for

router = APIRouter(tags=["mixes"])

MIX_STYLES = ("auto", "classic", "hype", "talking_head", "cinematic", "chill")


class MixCreate(BaseModel):
    target_duration_sec: float = Field(default=45.0, ge=15.0, le=300.0)
    prompt: str | None = Field(default=None, max_length=500)
    style: str = "auto"
    aspect: str = "9:16"
    fps: int = 30

    @field_validator("prompt", mode="before")
    @classmethod
    def _clean_prompt(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("style")
    @classmethod
    def _valid_style(cls, v):
        if v not in MIX_STYLES:
            raise ValueError(f"style must be one of {MIX_STYLES}")
        return v


@router.post("/projects/{project_id}/mixes", response_model=JobOut)
async def create_mix(
    project_id: str,
    body: MixCreate,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    project = await db.get(dbmod.Project, project_id)
    if project is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")

    assets = (
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
    analyzed = [
        a
        for a in assets
        if (working_dir_for(a.id) / "analysis.json").exists()
        and Path(a.path).exists()
    ]
    if len(analyzed) < 2:
        raise ApiError(
            409,
            "NOT_ENOUGH_CLIPS",
            "An AI mix needs at least 2 analyzed clips — a single clip "
            "already gets reels from selection.",
        )

    # Primary = the longest analyzed clip; its working dir hosts the render
    # and it must also have a probe.json for the worker's asset load.
    primary = max(analyzed, key=lambda a: a.duration_sec)
    if not (working_dir_for(primary.id) / "probe.json").exists():
        raise ApiError(
            409, "ASSET_NOT_READY", f"primary clip {primary.id[:12]} has no probe.json"
        )

    mix_id = f"mix-{uuid.uuid4().hex[:12]}"
    row = dbmod.Reel(
        id=mix_id,
        project_id=project_id,
        asset_id=primary.id,
        rank=0,
        title="AI mix (working…)",
        hook="",
        justification="AI mix",
        start_sec=0.0,
        end_sec=body.target_duration_sec,
        duration_sec=body.target_duration_sec,
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
        # NOTE: no mezzanine_path pre-write — GET /reels/{id} syncs it from
        # working/{primary}/reels/{mix_id}/mezzanine.mp4 once composed.
    )
    db.add(row)
    await db.commit()

    sources = [(a.id, a.path, a.original_filename) for a in analyzed]
    job_row = await enqueue_job(
        db,
        arq,
        project_id=project_id,
        kind="compose",
        function_name="create_mix_job",
        function_args=[
            project_id,
            mix_id,
            primary.id,
            sources,
            body.model_dump(),
        ],
        config=body,
        asset_id=primary.id,
        reel_id=mix_id,
        conflict_filter=(dbmod.Job.kind == "compose") & (dbmod.Job.reel_id == mix_id),
    )
    return JobOut(**await job_with_live_progress(db, None, job_row.id))


@router.get("/projects/{project_id}/mixes")
async def list_mixes(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    rows = (
        (
            await db.execute(
                select(dbmod.Reel).where(
                    dbmod.Reel.project_id == project_id,
                    dbmod.Reel.id.like("mix-%"),
                )
            )
        )
        .scalars()
        .all()
    )
    out = []
    for r in sorted(rows, key=lambda x: x.created_at, reverse=True):
        mezz = working_dir_for(r.asset_id) / "reels" / r.id / "mezzanine.mp4"
        out.append(
            {
                "id": r.id,
                "title": r.title,
                "hook": r.hook,
                "duration_sec": r.duration_sec,
                "suggested_mood": r.suggested_mood,
                "edit_style": r.edit_style,
                "mezzanine_ready": mezz.exists(),
                "created_at": r.created_at.isoformat(),
            }
        )
    return {"mixes": out}
