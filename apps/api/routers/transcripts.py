"""GET/PUT/DELETE the user-editable transcript override for an asset."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.errors import ApiError
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.models import Transcript
from reelforge_core.transcript_store import (
    delete_override,
    load_override,
    save_override,
    validate_transcript,
)

router = APIRouter(tags=["transcripts"])


@router.get("/assets/{asset_id}/transcript")
async def get_transcript(
    asset_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    # Prefer override.
    ovr = await load_override(asset_id)
    if ovr is not None:
        return {"transcript": ovr.model_dump(), "source": "override"}
    # Fall back to Phase 1 transcript.json
    wd = working_dir_for(asset_id)
    tpath = wd / "transcript.json"
    if not tpath.exists():
        raise ApiError(
            404, "ANALYSIS_NOT_READY", f"no transcript available for asset {asset_id}"
        )
    raw = json.loads(tpath.read_text())
    if raw.get("transcript") is None:
        return {"transcript": None, "source": "whisper"}
    return {"transcript": raw, "source": "whisper"}


@router.put("/assets/{asset_id}/transcript")
async def put_transcript(
    asset_id: str,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    try:
        transcript = Transcript.model_validate(body)
    except Exception as exc:
        raise ApiError(400, "INVALID_CONFIG", f"transcript failed Pydantic validation: {exc}")
    try:
        validate_transcript(transcript)
    except ValueError as exc:
        raise ApiError(400, "INVALID_CONFIG", str(exc))
    await save_override(asset_id, transcript)
    return {"transcript": transcript.model_dump(), "source": "override"}


@router.delete("/assets/{asset_id}/transcript", status_code=204)
async def reset_transcript(
    asset_id: str, db: AsyncSession = Depends(get_db)
) -> None:
    a = await db.get(dbmod.Asset, asset_id)
    if a is None:
        raise ApiError(404, "ASSET_NOT_FOUND", f"asset {asset_id} not found")
    await delete_override(asset_id)
