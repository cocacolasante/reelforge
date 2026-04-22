"""Export enqueue + list + detail."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import ExportCreate, ExportList, ExportOut, JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.export import PRESETS

router = APIRouter(tags=["exports"])


async def _load_reel(db: AsyncSession, reel_id: str) -> dbmod.Reel:
    r = await db.get(dbmod.Reel, reel_id)
    if r is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")
    return r


def _to_export_out(e: dbmod.Export) -> ExportOut:
    return ExportOut(
        id=e.id,
        reel_id=e.reel_id,
        preset_id=e.preset_id,
        output_path=e.output_path,
        file_size_bytes=e.file_size_bytes,
        created_at=e.created_at,
    )


@router.post("/reels/{reel_id}/exports", response_model=JobOut)
async def enqueue_export(
    reel_id: str,
    body: ExportCreate,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    reel = await _load_reel(db, reel_id)
    if body.preset_id not in PRESETS:
        raise ApiError(
            400,
            "INVALID_PRESET",
            f"unknown preset {body.preset_id!r}",
            valid=list(PRESETS),
        )

    mezz = working_dir_for(reel.asset_id) / "reels" / reel_id / "mezzanine.mp4"
    if not mezz.exists():
        raise ApiError(
            409, "MEZZANINE_NOT_READY", f"mezzanine missing for reel {reel_id}"
        )

    job_row = await enqueue_job(
        db,
        arq,
        project_id=reel.project_id,
        kind="export",
        function_name="export_reel_job",
        function_args=[reel.asset_id, reel_id, body.preset_id, body.force],
        config=None,
        asset_id=reel.asset_id,
        reel_id=reel_id,
        preset_id=body.preset_id,
        conflict_filter=(
            (dbmod.Job.kind == "export")
            & (dbmod.Job.reel_id == reel_id)
            & (dbmod.Job.preset_id == body.preset_id)
        ),
    )

    # Upsert an Export row pointing at the expected output path.
    preset = PRESETS[body.preset_id]
    expected_path = (
        working_dir_for(reel.asset_id).parent.parent
        / "outputs"
        / reel.asset_id
        / reel_id
        / f"{body.preset_id}.{preset.container}"
    )
    existing = (
        await db.execute(
            select(dbmod.Export).where(
                dbmod.Export.reel_id == reel_id,
                dbmod.Export.preset_id == body.preset_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            dbmod.Export(
                reel_id=reel_id,
                preset_id=body.preset_id,
                export_job_id=job_row.id,
                output_path=None,  # filled after worker finishes
            )
        )
    else:
        existing.export_job_id = job_row.id
    await db.commit()

    return JobOut(**await job_with_live_progress(db, None, job_row.id))


@router.get("/reels/{reel_id}/exports", response_model=ExportList)
async def list_exports(
    reel_id: str, db: AsyncSession = Depends(get_db)
) -> ExportList:
    _ = await _load_reel(db, reel_id)
    rows = (
        await db.execute(
            select(dbmod.Export).where(dbmod.Export.reel_id == reel_id)
        )
    ).scalars().all()
    # Sync output_path / size from disk if the worker already finished.
    for e in rows:
        if e.output_path and Path(e.output_path).exists():
            size = Path(e.output_path).stat().st_size
            if e.file_size_bytes != size:
                e.file_size_bytes = size
        else:
            # Try to locate the file (the worker might have finished between rows).
            preset = PRESETS.get(e.preset_id)
            if preset is None:
                continue
            reel = await db.get(dbmod.Reel, reel_id)
            if reel is None:
                continue
            candidate = (
                working_dir_for(reel.asset_id).parent.parent
                / "outputs"
                / reel.asset_id
                / reel_id
                / f"{e.preset_id}.{preset.container}"
            )
            if candidate.exists():
                e.output_path = str(candidate)
                e.file_size_bytes = candidate.stat().st_size
    await db.commit()
    return ExportList(exports=[_to_export_out(e) for e in rows])


@router.get("/exports/{export_id}", response_model=ExportOut)
async def get_export(
    export_id: str, db: AsyncSession = Depends(get_db)
) -> ExportOut:
    e = await db.get(dbmod.Export, export_id)
    if e is None:
        raise ApiError(404, "EXPORT_NOT_FOUND", f"export {export_id} not found")
    if e.output_path and Path(e.output_path).exists():
        e.file_size_bytes = Path(e.output_path).stat().st_size
        await db.commit()
    return _to_export_out(e)
