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
from reelforge_core.models import (
    AnalysisReport,
    ReelSelection,
    ReelTimeline,
    TimelineShot,
)

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


def _edited_duration(r: dbmod.Reel) -> Optional[float]:
    if not r.edit_json:
        return None
    try:
        return round(ReelTimeline.model_validate_json(r.edit_json).total_duration, 3)
    except Exception:
        return None


def _to_reel_out(r: dbmod.Reel, mezz_ready: bool) -> ReelOut:
    return ReelOut(
        has_edits=bool(r.edit_json),
        edited_duration_sec=_edited_duration(r),
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
        prompt_relevance=r.prompt_relevance,
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



# ---------------------------------------------------------------------------
# Editable timeline
# ---------------------------------------------------------------------------


class SceneOut(BaseModel):
    index: int
    start_sec: float
    end_sec: float
    thumbnail_url: str


class SourceVideoOut(BaseModel):
    asset_id: str
    filename: str
    duration_sec: float
    width: int
    height: int
    analyzed: bool
    scenes: list[SceneOut]


class SourcePhotoOut(BaseModel):
    asset_id: str
    filename: str
    url: str


class SourceAudioOut(BaseModel):
    asset_id: str
    filename: str
    duration_sec: float
    url: str


class ReelEditOut(BaseModel):
    reel_id: str
    has_edits: bool
    timeline: ReelTimeline
    videos: list[SourceVideoOut]
    photos: list[SourcePhotoOut]
    audios: list[SourceAudioOut] = []


class ReelEditIn(BaseModel):
    timeline: ReelTimeline


MAX_TIMELINE_SHOTS = 60
MAX_OVERLAYS = 40


def _default_timeline(r: dbmod.Reel) -> ReelTimeline:
    """The AI cut as an editable timeline: one video shot per scene, with the
    reel's saved trim offsets folded into the outer bounds."""
    wd = working_dir_for(r.asset_id)
    scene_indices = json.loads(r.scene_indices_json)
    shots: list[TimelineShot] = []
    analysis_path = wd / "analysis.json"
    scenes_by_idx: dict[int, tuple[float, float]] = {}
    if analysis_path.exists():
        try:
            rep = AnalysisReport.model_validate_json(analysis_path.read_text())
            scenes_by_idx = {s.index: (s.start_sec, s.end_sec) for s in rep.scenes}
        except Exception:
            scenes_by_idx = {}
    for pos, idx in enumerate(scene_indices):
        start, end = scenes_by_idx.get(idx, (r.start_sec, r.end_sec))
        if pos == 0:
            start = max(0.0, start + r.trim_start_offset_sec)
        if pos == len(scene_indices) - 1:
            end = max(start + 0.1, end - r.trim_end_offset_sec)
        shots.append(
            TimelineShot(kind="video", asset_id=r.asset_id, in_ts=start, out_ts=end)
        )
    if not shots:  # montage rows or missing analysis
        shots.append(
            TimelineShot(kind="video", asset_id=r.asset_id, in_ts=r.start_sec, out_ts=r.end_sec)
        )
    return ReelTimeline(shots=shots)


async def _project_sources(
    db: AsyncSession, project_id: str
) -> tuple[list[SourceVideoOut], list[SourcePhotoOut], list[SourceAudioOut]]:
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(dbmod.Asset)
            .where(dbmod.Asset.project_id == project_id)
            .order_by(dbmod.Asset.created_at)
        )
    ).scalars().all()
    videos: list[SourceVideoOut] = []
    photos: list[SourcePhotoOut] = []
    audios: list[SourceAudioOut] = []
    for a in rows:
        if a.kind == "audio":
            audios.append(
                SourceAudioOut(
                    asset_id=a.id,
                    filename=a.original_filename,
                    duration_sec=a.duration_sec,
                    url=f"/api/v1/assets/{a.id}/media",
                )
            )
            continue
        if a.kind == "photo":
            photos.append(
                SourcePhotoOut(
                    asset_id=a.id,
                    filename=a.original_filename,
                    url=f"/api/v1/assets/{a.id}/photo",
                )
            )
            continue
        scenes: list[SceneOut] = []
        analysis_path = working_dir_for(a.id) / "analysis.json"
        analyzed = analysis_path.exists()
        if analyzed:
            try:
                rep = AnalysisReport.model_validate_json(analysis_path.read_text())
                scenes = [
                    SceneOut(
                        index=sc.index,
                        start_sec=sc.start_sec,
                        end_sec=sc.end_sec,
                        thumbnail_url=f"/api/v1/assets/{a.id}/thumbnails/{sc.index}",
                    )
                    for sc in rep.scenes
                ]
            except Exception:
                scenes = []
        videos.append(
            SourceVideoOut(
                asset_id=a.id,
                filename=a.original_filename,
                duration_sec=a.duration_sec,
                width=a.width,
                height=a.height,
                analyzed=analyzed,
                scenes=scenes,
            )
        )
    return videos, photos, audios


@router.get("/reels/{reel_id}/edit", response_model=ReelEditOut)
async def get_reel_edit(reel_id: str, db: AsyncSession = Depends(get_db)) -> ReelEditOut:
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    if r.edit_json:
        try:
            timeline = ReelTimeline.model_validate_json(r.edit_json)
        except Exception:
            timeline = _default_timeline(r)
    else:
        timeline = _default_timeline(r)
    # Never leak on-disk paths to the client; they're resolved at compose time.
    timeline = timeline.model_copy(
        update={
            "shots": [s.model_copy(update={"path": ""}) for s in timeline.shots],
            "voiceovers": [v.model_copy(update={"path": ""}) for v in timeline.voiceovers],
        }
    )
    videos, photos, audios = await _project_sources(db, r.project_id)
    return ReelEditOut(
        reel_id=reel_id,
        has_edits=bool(r.edit_json),
        timeline=timeline,
        videos=videos,
        photos=photos,
        audios=audios,
    )


@router.put("/reels/{reel_id}/edit", response_model=ReelEditOut)
async def put_reel_edit(
    reel_id: str, body: ReelEditIn, db: AsyncSession = Depends(get_db)
) -> ReelEditOut:
    """Save an edited timeline. Validates every shot against the project's
    assets so a render can never reference someone else's footage."""
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    tl = body.timeline
    if not tl.shots:
        raise ApiError(400, "INVALID_CONFIG", "a timeline needs at least one shot")
    if len(tl.shots) > MAX_TIMELINE_SHOTS:
        raise ApiError(400, "INVALID_CONFIG", f"at most {MAX_TIMELINE_SHOTS} shots")
    if len(tl.overlays) > MAX_OVERLAYS:
        raise ApiError(400, "INVALID_CONFIG", f"at most {MAX_OVERLAYS} text overlays")

    for i, shot in enumerate(tl.shots):
        a = await db.get(dbmod.Asset, shot.asset_id)
        if a is None or a.project_id != r.project_id:
            raise ApiError(
                400, "INVALID_CONFIG", f"shot {i + 1}: asset is not part of this project"
            )
        if shot.kind == "photo":
            if a.kind != "photo":
                raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: {a.original_filename} is not a photo")
            if not (0.2 <= shot.duration_sec <= 30):
                raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: photo duration must be 0.2-30s")
        else:
            if a.kind == "photo":
                raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: {a.original_filename} is a photo, not footage")
            if shot.in_ts < 0 or shot.out_ts <= shot.in_ts:
                raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: out must be after in")
            if shot.out_ts - shot.in_ts < 0.5:
                raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: shots must be at least 0.5s")
            if a.duration_sec and shot.out_ts > a.duration_sec + 0.05:
                raise ApiError(
                    400, "INVALID_CONFIG",
                    f"shot {i + 1}: out ({shot.out_ts:.1f}s) is past the end of "
                    f"{a.original_filename} ({a.duration_sec:.1f}s)",
                )
        if shot.transition_after is not None and shot.transition_after.kind == "auto":
            raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: per-cut transition can't be 'auto'")

    total = tl.total_duration
    for i, shot in enumerate(tl.shots):
        if not (0.0 <= shot.volume <= 3.0):
            raise ApiError(400, "INVALID_CONFIG", f"shot {i + 1}: volume must be 0-3")
    for k, take in enumerate(tl.voiceovers):
        a = await db.get(dbmod.Asset, take.asset_id)
        if a is None or a.project_id != r.project_id or a.kind != "audio":
            raise ApiError(
                400, "INVALID_CONFIG", f"voiceover {k + 1}: not an audio take in this project"
            )
        if take.start_sec < 0:
            raise ApiError(400, "INVALID_CONFIG", f"voiceover {k + 1}: start must be >= 0")
        if take.start_sec > total:
            raise ApiError(
                400, "INVALID_CONFIG",
                f"voiceover {k + 1}: starts at {take.start_sec:.1f}s but the reel is {total:.1f}s",
            )
        if not (0.0 <= take.volume <= 3.0):
            raise ApiError(400, "INVALID_CONFIG", f"voiceover {k + 1}: volume must be 0-3")
    for j, o in enumerate(tl.overlays):
        if not o.text.strip():
            raise ApiError(400, "INVALID_CONFIG", f"overlay {j + 1}: text is empty")
        if o.start_sec < 0 or o.end_sec <= o.start_sec:
            raise ApiError(400, "INVALID_CONFIG", f"overlay {j + 1}: end must be after start")
        if o.start_sec > total:
            raise ApiError(
                400, "INVALID_CONFIG",
                f"overlay {j + 1}: starts at {o.start_sec:.1f}s but the reel is {total:.1f}s",
            )

    # Store without paths; compose resolves them fresh from the DB.
    clean = tl.model_copy(
        update={
            "shots": [s.model_copy(update={"path": ""}) for s in tl.shots],
            "voiceovers": [v.model_copy(update={"path": ""}) for v in tl.voiceovers],
        }
    )
    r.edit_json = clean.model_dump_json()
    await db.commit()
    videos, photos, audios = await _project_sources(db, r.project_id)
    return ReelEditOut(
        reel_id=reel_id, has_edits=True, timeline=clean, videos=videos, photos=photos, audios=audios
    )


@router.delete("/reels/{reel_id}/edit", status_code=204)
async def reset_reel_edit(reel_id: str, db: AsyncSession = Depends(get_db)) -> None:
    """Discard edits and return to the AI cut."""
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    r.edit_json = None
    await db.commit()
