"""Social publishing: account connect (OAuth) + publish enqueue + status.

YouTube only for now. The OAuth client credentials come from the user's own
Google Cloud project via GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — see
docs/publishing.md for the one-time setup.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_arq, get_db
from apps.api.schemas.common import JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import enqueue_job, job_with_live_progress
from apps.api.settings import settings
from reelforge_core.publish import youtube

router = APIRouter(tags=["social"])

# In-memory OAuth state store (single-user app; states expire after 10 min).
_pending_states: dict[str, float] = {}
_STATE_TTL = 600.0


def _redirect_uri() -> str:
    return f"{settings.public_api_base}/api/v1/social/youtube/callback"


def _require_google_config() -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ApiError(
            409,
            "SOCIAL_NOT_CONFIGURED",
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set — "
            "see docs/publishing.md for setup.",
        )


# ---- schemas ---------------------------------------------------------------


class SocialAccountOut(BaseModel):
    platform: str
    display_name: str | None
    connected_at: datetime


class SocialAccountList(BaseModel):
    accounts: list[SocialAccountOut]


class PublishCreate(BaseModel):
    platform: str = "youtube"
    preset_id: str = "mp4_h264_social"
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4900)
    privacy: str = Field(default="private", pattern="^(private|unlisted|public)$")


class PublicationOut(BaseModel):
    id: str
    reel_id: str
    platform: str
    preset_id: str
    title: str
    privacy: str
    status: str
    publish_job_id: str | None
    video_id: str | None
    video_url: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class PublicationList(BaseModel):
    publications: list[PublicationOut]


def _to_pub_out(p: dbmod.Publication) -> PublicationOut:
    return PublicationOut(
        id=p.id,
        reel_id=p.reel_id,
        platform=p.platform,
        preset_id=p.preset_id,
        title=p.title,
        privacy=p.privacy,
        status=p.status,
        publish_job_id=p.publish_job_id,
        video_id=p.video_id,
        video_url=p.video_url,
        error_message=p.error_message,
        created_at=p.created_at,
        completed_at=p.completed_at,
    )


# ---- account endpoints -----------------------------------------------------


@router.get("/social/accounts", response_model=SocialAccountList)
async def list_accounts(db: AsyncSession = Depends(get_db)) -> SocialAccountList:
    rows = (await db.execute(select(dbmod.SocialAccount))).scalars().all()
    return SocialAccountList(
        accounts=[
            SocialAccountOut(
                platform=r.platform,
                display_name=r.display_name,
                connected_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get("/social/youtube/connect")
async def youtube_connect() -> RedirectResponse:
    _require_google_config()
    now = time.monotonic()
    for s, t in list(_pending_states.items()):
        if now - t > _STATE_TTL:
            del _pending_states[s]
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now
    return RedirectResponse(
        youtube.auth_url(settings.google_client_id, _redirect_uri(), state)
    )


@router.get("/social/youtube/callback")
async def youtube_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if error or not code:
        return RedirectResponse(f"{settings.web_base}/?youtube_error={error or 'denied'}")
    if state not in _pending_states:
        raise ApiError(400, "INVALID_CONFIG", "unknown OAuth state — retry the connect flow")
    del _pending_states[state]
    _require_google_config()

    tokens = await asyncio.to_thread(
        youtube.exchange_code,
        settings.google_client_id,
        settings.google_client_secret,
        code,
        _redirect_uri(),
    )
    try:
        title = await asyncio.to_thread(youtube.fetch_channel_title, tokens["access_token"])
    except youtube.PublishError:
        title = None

    existing = (
        await db.execute(
            select(dbmod.SocialAccount).where(dbmod.SocialAccount.platform == "youtube")
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            dbmod.SocialAccount(
                platform="youtube",
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                display_name=title,
            )
        )
    else:
        existing.access_token = tokens["access_token"]
        existing.refresh_token = tokens["refresh_token"]
        existing.display_name = title or existing.display_name
    await db.commit()
    return RedirectResponse(f"{settings.web_base}/?connected=youtube")


@router.delete("/social/youtube", status_code=204)
async def youtube_disconnect(db: AsyncSession = Depends(get_db)) -> None:
    existing = (
        await db.execute(
            select(dbmod.SocialAccount).where(dbmod.SocialAccount.platform == "youtube")
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.delete(existing)
        await db.commit()


# ---- publish ---------------------------------------------------------------


@router.post("/reels/{reel_id}/publish", response_model=JobOut)
async def publish_reel(
    reel_id: str,
    body: PublishCreate,
    db: AsyncSession = Depends(get_db),
    arq: ArqRedis = Depends(get_arq),
) -> JobOut:
    if body.platform != "youtube":
        raise ApiError(400, "INVALID_CONFIG", f"unsupported platform {body.platform!r}")
    reel = await db.get(dbmod.Reel, reel_id)
    if reel is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")

    account = (
        await db.execute(
            select(dbmod.SocialAccount).where(dbmod.SocialAccount.platform == "youtube")
        )
    ).scalar_one_or_none()
    if account is None:
        raise ApiError(
            409,
            "SOCIAL_NOT_CONNECTED",
            "No YouTube account connected — connect one first.",
        )

    from reelforge_core.export import PRESETS

    preset = PRESETS.get(body.preset_id)
    if preset is None:
        raise ApiError(400, "INVALID_PRESET", f"unknown preset {body.preset_id!r}")
    if preset.container != "mp4":
        raise ApiError(
            400, "INVALID_PRESET", "publish uses an MP4 preset (ProRes is editorial-only)"
        )
    output = (
        settings.data_dir
        / "outputs"
        / reel.asset_id
        / reel_id
        / f"{body.preset_id}.{preset.container}"
    )
    if not output.exists():
        raise ApiError(
            409,
            "EXPORT_NOT_READY",
            f"export {body.preset_id} has not been produced for this reel — export first.",
        )

    pub = dbmod.Publication(
        reel_id=reel_id,
        platform="youtube",
        preset_id=body.preset_id,
        title=body.title,
        description=body.description,
        privacy=body.privacy,
    )
    db.add(pub)
    await db.commit()

    job_row = await enqueue_job(
        db,
        arq,
        kind="publish",
        function_name="publish_reel_job",
        function_args=[pub.id],
        project_id=reel.project_id,
        asset_id=reel.asset_id,
        reel_id=reel_id,
        conflict_filter=(dbmod.Job.kind == "publish") & (dbmod.Job.reel_id == reel_id),
    )
    pub.publish_job_id = job_row.id
    await db.commit()
    return JobOut(**await job_with_live_progress(db, None, job_row.id))


@router.get("/reels/{reel_id}/publications", response_model=PublicationList)
async def list_publications(
    reel_id: str, db: AsyncSession = Depends(get_db)
) -> PublicationList:
    rows = (
        await db.execute(
            select(dbmod.Publication)
            .where(dbmod.Publication.reel_id == reel_id)
            .order_by(dbmod.Publication.created_at.desc())
        )
    ).scalars().all()
    return PublicationList(publications=[_to_pub_out(p) for p in rows])
