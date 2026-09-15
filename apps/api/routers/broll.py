"""AI B-roll suggestions for the timeline editor.

`POST /reels/{id}/broll/suggest` enqueues one `suggest_broll_job` (job kind
"broll") against the editor's CURRENT timeline — unsaved edits included —
falling back to the saved edit or the AI cut. The job result carries
validated layer suggestions; nothing is written to the reel until the user
accepts them in the editor and saves.
"""

from __future__ import annotations

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.routers.reels import _default_timeline
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from reelforge_core.models import ReelTimeline

router = APIRouter(tags=["broll"])


class BrollSuggestIn(BaseModel):
    timeline: ReelTimeline | None = None
    prompt: str | None = Field(default=None, max_length=500)

    @field_validator("prompt", mode="before")
    @classmethod
    def _clean_prompt(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None


@router.post("/reels/{reel_id}/broll/suggest", response_model=JobOut)
async def suggest_broll(
    reel_id: str,
    body: BrollSuggestIn | None = None,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    body = body or BrollSuggestIn()
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    tl = body.timeline
    if tl is None:
        try:
            tl = ReelTimeline.model_validate_json(r.edit_json) if r.edit_json else _default_timeline(r)
        except Exception:
            tl = _default_timeline(r)
    if not tl.shots:
        raise ApiError(400, "INVALID_CONFIG", "a timeline needs at least one shot")

    rows = (
        await db.execute(select(dbmod.Asset).where(dbmod.Asset.project_id == r.project_id))
    ).scalars().all()
    project_ids = {a.id for a in rows}
    for item in [*tl.shots, *tl.layers]:
        if item.asset_id not in project_ids:
            raise ApiError(400, "INVALID_CONFIG", "the timeline uses a clip that isn't part of this project")

    videos = [(a.id, a.original_filename) for a in rows if a.kind not in ("photo", "audio")]
    photos = [(a.id, a.original_filename, a.path) for a in rows if a.kind == "photo"]
    # Paths never travel from the client; the worker only needs asset ids.
    clean = tl.model_copy(
        update={
            "shots": [s.model_copy(update={"path": ""}) for s in tl.shots],
            "voiceovers": [v.model_copy(update={"path": ""}) for v in tl.voiceovers],
            "layers": [ly.model_copy(update={"path": ""}) for ly in tl.layers],
        }
    )
    job_row = await enqueue_job(
        db,
        arq,
        project_id=r.project_id,
        kind="broll",
        function_name="suggest_broll_job",
        function_args=[r.project_id, reel_id, clean.model_dump(), videos, photos, body.prompt],
        conflict_filter=(dbmod.Job.reel_id == reel_id) & (dbmod.Job.kind == "broll"),
        reel_id=reel_id,
    )
    return JobOut(**await job_with_live_progress(db, None, job_row.id))
