"""Long-form montage: stitch N composed reels into one mezzanine.

The resulting montage is stored as a Reel row with `child_reel_ids_json` set
and a synthetic id `montage-{hex}`. That lets the existing export pipeline
treat it like any other reel.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress

router = APIRouter(tags=["montages"])


class MontageCreate(BaseModel):
    reel_ids: list[str] = Field(min_length=1)
    transition_duration_sec: float = 0.6
    aspect: str = "9:16"
    fps: int = 30
    title: str | None = None


def _aspect_resolution(aspect: str) -> tuple[int, int]:
    return {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }.get(aspect, (1080, 1920))


@router.post("/projects/{project_id}/montages", response_model=JobOut)
async def create_montage(
    project_id: str,
    body: MontageCreate,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    project = await db.get(dbmod.Project, project_id)
    if project is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")

    # Resolve child reels — must all belong to this project, all have a
    # mezzanine on disk.
    chapters: list[tuple[dbmod.Reel, Path]] = []
    for rid in body.reel_ids:
        reel = await db.get(dbmod.Reel, rid)
        if reel is None or reel.project_id != project_id:
            raise ApiError(404, "REEL_NOT_FOUND", f"reel {rid} not in project {project_id}")
        if not reel.mezzanine_path or not Path(reel.mezzanine_path).exists():
            raise ApiError(
                409,
                "MEZZANINE_NOT_READY",
                f"reel {rid} has no composed mezzanine yet",
                reel_id=rid,
            )
        chapters.append((reel, Path(reel.mezzanine_path)))

    w, h = _aspect_resolution(body.aspect)
    montage_id = f"montage-{uuid.uuid4().hex[:12]}"
    # The output path is deterministic from project + montage id; we pre-write
    # the Reel row pointing at it so the UI sees the montage immediately.
    from apps.api.settings import settings

    out_path = (
        settings.data_dir / "working" / "_montage" / project_id / montage_id / "mezzanine.mp4"
    )

    # Pick the first chapter's asset for thumbnails + duration-summarisation.
    primary_asset_id = chapters[0][0].asset_id
    title = body.title or f"Montage of {len(chapters)} reels"
    # Aggregate stats from the children
    total_duration = sum((c[0].duration_sec for c in chapters), 0.0) - max(
        0, len(chapters) - 1
    ) * body.transition_duration_sec
    mean_score = sum(c[0].overall_score for c in chapters) / max(1, len(chapters))

    db.add(
        dbmod.Reel(
            id=montage_id,
            project_id=project_id,
            asset_id=primary_asset_id,
            rank=0,
            title=title,
            hook="",
            justification="",
            start_sec=0.0,
            end_sec=total_duration,
            duration_sec=total_duration,
            overall_score=mean_score,
            suggested_mood="neutral",
            scene_indices_json="[]",
            scores_json=json.dumps(
                {
                    "narrative_coherence": int(mean_score),
                    "hook_strength": int(mean_score),
                    "emotional_payoff": int(mean_score),
                    "standalone_clarity": int(mean_score),
                }
            ),
            mezzanine_path=str(out_path),  # will exist once the job completes
            child_reel_ids_json=json.dumps([c[0].id for c in chapters]),
        )
    )
    await db.commit()

    # Smart picker: derive the xfade kind for the chapter boundaries from the
    # children's moods. We pass the kind explicitly to the worker so the job
    # doesn't need to re-fetch them.
    from reelforge_core.compose.auto import pick_transition_kind_for_montage

    transition_kind = pick_transition_kind_for_montage(
        c[0].suggested_mood for c in chapters
    )

    job_row = await enqueue_job(
        db,
        arq,
        project_id=project_id,
        kind="compose",
        function_name="compile_montage_job",
        function_args=[
            project_id,
            montage_id,
            [str(p) for _, p in chapters],
            body.transition_duration_sec,
            w,
            h,
            body.fps,
            transition_kind,
        ],
        config=None,
        asset_id=primary_asset_id,
        reel_id=montage_id,
        conflict_filter=(
            (dbmod.Job.kind == "compose") & (dbmod.Job.reel_id == montage_id)
        ),
    )
    return JobOut(**await job_with_live_progress(db, None, job_row.id))


@router.get("/projects/{project_id}/montages")
async def list_montages(
    project_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    project = await db.get(dbmod.Project, project_id)
    if project is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", f"project {project_id} not found")
    rows = (
        await db.execute(
            select(dbmod.Reel).where(
                dbmod.Reel.project_id == project_id,
                dbmod.Reel.child_reel_ids_json.is_not(None),
            )
        )
    ).scalars().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        mezz_ready = bool(r.mezzanine_path and Path(r.mezzanine_path).exists())
        out.append(
            {
                "id": r.id,
                "title": r.title,
                "duration_sec": r.duration_sec,
                "child_reel_ids": json.loads(r.child_reel_ids_json or "[]"),
                "mezzanine_ready": mezz_ready,
                "created_at": r.created_at,
            }
        )
    out.sort(key=lambda m: m["created_at"], reverse=True)
    return {"montages": out}
