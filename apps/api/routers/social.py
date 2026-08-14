"""Social publishing: account connect (OAuth) + publish enqueue + status.

Platforms: YouTube (resumable upload), Instagram Reels (Meta fetches the
video from our tokened public media route — requires a tunnel), TikTok
(direct file upload to the user's TikTok inbox; they finish in-app). OAuth
client credentials come from the user's own developer apps on each platform —
see docs/publishing.md for the one-time setups.
"""

from __future__ import annotations

import asyncio
import json as _json
import secrets
import time
from datetime import datetime, timedelta, timezone

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Request
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
from apps.api.streaming import stream_file_with_range
from reelforge_core.publish import instagram, tiktok, youtube

SUPPORTED_PLATFORMS = ("youtube", "instagram", "tiktok")

import logging
from urllib.parse import quote, urlencode

router = APIRouter(tags=["social"])
log = logging.getLogger(__name__)

# In-memory OAuth state store (single-user app; states expire after 10 min).
# Maps state -> (created_monotonic, next_path) so the callback can send the
# user back to the page they started from.
_pending_states: dict[str, tuple[float, str]] = {}
_STATE_TTL = 600.0


def _redirect_uri(platform: str = "youtube") -> str:
    return f"{settings.public_api_base}/api/v1/social/{platform}/callback"


def _require_google_config() -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ApiError(
            409,
            "SOCIAL_NOT_CONFIGURED",
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set — "
            "see docs/publishing.md for setup.",
        )


def _require_instagram_config() -> None:
    if not settings.instagram_app_id or not settings.instagram_app_secret:
        raise ApiError(
            409,
            "SOCIAL_NOT_CONFIGURED",
            "INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET are not set — "
            "see docs/publishing.md for setup.",
        )


def _require_tiktok_config() -> None:
    if not settings.tiktok_client_key or not settings.tiktok_client_secret:
        raise ApiError(
            409,
            "SOCIAL_NOT_CONFIGURED",
            "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET are not set — "
            "see docs/publishing.md for setup.",
        )


def _register_state(next_path: str) -> str:
    now = time.monotonic()
    for s, (t, _) in list(_pending_states.items()):
        if now - t > _STATE_TTL:
            del _pending_states[s]
    state = secrets.token_urlsafe(24)
    safe = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    _pending_states[state] = (now, safe)
    return state


async def _upsert_account(
    db: AsyncSession,
    *,
    platform: str,
    external_id: str,
    access_token: str,
    refresh_token: str,
    display_name: str | None,
    token_expires_at: datetime | None = None,
    extra_json: str | None = None,
) -> None:
    existing = (
        await db.execute(
            select(dbmod.SocialAccount).where(
                dbmod.SocialAccount.platform == platform,
                dbmod.SocialAccount.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            dbmod.SocialAccount(
                platform=platform,
                external_id=external_id,
                access_token=access_token,
                refresh_token=refresh_token,
                display_name=display_name,
                token_expires_at=token_expires_at,
                extra_json=extra_json,
            )
        )
    else:
        existing.access_token = access_token
        existing.refresh_token = refresh_token
        existing.display_name = display_name or existing.display_name
        existing.token_expires_at = token_expires_at
        if extra_json is not None:
            existing.extra_json = extra_json
    await db.commit()


# ---- schemas ---------------------------------------------------------------


class SocialAccountOut(BaseModel):
    id: str
    platform: str
    external_id: str
    display_name: str | None
    connected_at: datetime


class SocialAccountList(BaseModel):
    accounts: list[SocialAccountOut]


class PublishCreate(BaseModel):
    platform: str = "youtube"
    # Which connected channel to publish to. Optional only when exactly one
    # channel is connected.
    account_id: str | None = None
    preset_id: str = "mp4_h264_social"
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=4900)
    privacy: str = Field(default="private", pattern="^(private|unlisted|public)$")


class PublicationOut(BaseModel):
    id: str
    reel_id: str
    platform: str
    channel_title: str | None
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
        channel_title=p.channel_title,
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
    rows = (
        await db.execute(
            select(dbmod.SocialAccount).order_by(dbmod.SocialAccount.created_at)
        )
    ).scalars().all()
    return SocialAccountList(
        accounts=[
            SocialAccountOut(
                id=r.id,
                platform=r.platform,
                external_id=r.external_id,
                display_name=r.display_name,
                connected_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get("/social/youtube/connect")
async def youtube_connect(next: str = "/") -> RedirectResponse:
    _require_google_config()
    state = _register_state(next)
    return RedirectResponse(
        youtube.auth_url(settings.google_client_id, _redirect_uri("youtube"), state)
    )


@router.get("/social/instagram/connect")
async def instagram_connect(next: str = "/") -> RedirectResponse:
    _require_instagram_config()
    state = _register_state(next)
    return RedirectResponse(
        instagram.auth_url(settings.instagram_app_id, _redirect_uri("instagram"), state)
    )


@router.get("/social/tiktok/connect")
async def tiktok_connect(next: str = "/") -> RedirectResponse:
    _require_tiktok_config()
    state = _register_state(next)
    return RedirectResponse(
        tiktok.auth_url(settings.tiktok_client_key, _redirect_uri("tiktok"), state)
    )


@router.get("/social/youtube/callback")
async def youtube_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    next_path = "/"
    if state in _pending_states:
        _, next_path = _pending_states.pop(state)
    elif not error:
        raise ApiError(400, "INVALID_CONFIG", "unknown OAuth state — retry the connect flow")

    def _fail(msg: str) -> RedirectResponse:
        log.warning("youtube connect failed: %s", msg)
        return RedirectResponse(
            f"{settings.web_base}{next_path}?youtube_error={quote(msg[:300])}"
        )

    if error or not code:
        return _fail(f"Google returned: {error or 'no authorization code'}")
    _require_google_config()

    try:
        tokens = await asyncio.to_thread(
            youtube.exchange_code,
            settings.google_client_id,
            settings.google_client_secret,
            code,
            _redirect_uri(),
        )
        channel = await asyncio.to_thread(
            youtube.fetch_channel_info, tokens["access_token"]
        )
    except youtube.PublishError as exc:
        # Without a channel id we can't attribute uploads to the right
        # channel — surface the failure rather than storing a blind token.
        return _fail(str(exc))

    # Upsert per channel: the token is scoped to whichever channel the user
    # picked on Google's account-chooser screen.
    await _upsert_account(
        db,
        platform="youtube",
        external_id=channel["id"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        display_name=channel["title"],
    )
    log.info("youtube channel connected: %s", channel["title"])
    return RedirectResponse(
        f"{settings.web_base}{next_path}?"
        + urlencode({"connected": channel["title"]})
    )


@router.get("/social/instagram/callback")
async def instagram_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    next_path = "/"
    if state in _pending_states:
        _, next_path = _pending_states.pop(state)
    elif not error:
        raise ApiError(400, "INVALID_CONFIG", "unknown OAuth state — retry the connect flow")

    def _fail(msg: str) -> RedirectResponse:
        log.warning("instagram connect failed: %s", msg)
        return RedirectResponse(
            f"{settings.web_base}{next_path}?youtube_error={quote(msg[:300])}"
        )

    if error or not code:
        return _fail(f"Instagram returned: {error_description or error or 'no code'}")
    _require_instagram_config()

    try:
        tokens = await asyncio.to_thread(
            instagram.exchange_code,
            settings.instagram_app_id,
            settings.instagram_app_secret,
            code,
            _redirect_uri("instagram"),
        )
        profile = await asyncio.to_thread(
            instagram.fetch_profile, tokens["access_token"]
        )
    except youtube.PublishError as exc:
        return _fail(str(exc))

    await _upsert_account(
        db,
        platform="instagram",
        external_id=profile["user_id"],
        access_token=tokens["access_token"],
        refresh_token="",  # IG long-lived tokens self-refresh
        display_name=f"@{profile['username']}" if profile.get("username") else None,
        token_expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=int(tokens.get("expires_in", 60 * 24 * 3600))),
        extra_json=_json.dumps({"user_id": profile["user_id"]}),
    )
    log.info("instagram account connected: @%s", profile.get("username"))
    return RedirectResponse(
        f"{settings.web_base}{next_path}?"
        + urlencode({"connected": f"@{profile.get('username') or 'instagram'}"})
    )


@router.get("/social/tiktok/callback")
async def tiktok_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    next_path = "/"
    if state in _pending_states:
        _, next_path = _pending_states.pop(state)
    elif not error:
        raise ApiError(400, "INVALID_CONFIG", "unknown OAuth state — retry the connect flow")

    def _fail(msg: str) -> RedirectResponse:
        log.warning("tiktok connect failed: %s", msg)
        return RedirectResponse(
            f"{settings.web_base}{next_path}?youtube_error={quote(msg[:300])}"
        )

    if error or not code:
        return _fail(f"TikTok returned: {error_description or error or 'no code'}")
    _require_tiktok_config()

    try:
        tokens = await asyncio.to_thread(
            tiktok.exchange_code,
            settings.tiktok_client_key,
            settings.tiktok_client_secret,
            code,
            _redirect_uri("tiktok"),
        )
        profile = await asyncio.to_thread(tiktok.fetch_profile, tokens["access_token"])
    except youtube.PublishError as exc:
        return _fail(str(exc))

    await _upsert_account(
        db,
        platform="tiktok",
        external_id=profile["open_id"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        display_name=profile.get("display_name"),
        token_expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=int(tokens.get("expires_in", 86400))),
        extra_json=_json.dumps({"open_id": profile["open_id"]}),
    )
    log.info("tiktok account connected: %s", profile.get("display_name"))
    return RedirectResponse(
        f"{settings.web_base}{next_path}?"
        + urlencode({"connected": profile.get("display_name") or "TikTok"})
    )


@router.delete("/social/accounts/{account_id}", status_code=204)
async def disconnect_account(
    account_id: str, db: AsyncSession = Depends(get_db)
) -> None:
    existing = await db.get(dbmod.SocialAccount, account_id)
    if existing is None:
        raise ApiError(404, "ACCOUNT_NOT_FOUND", f"account {account_id} not found")
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
    if body.platform not in SUPPORTED_PLATFORMS:
        raise ApiError(400, "INVALID_CONFIG", f"unsupported platform {body.platform!r}")
    reel = await db.get(dbmod.Reel, reel_id)
    if reel is None:
        raise ApiError(404, "REEL_NOT_FOUND", f"reel {reel_id} not found")

    if body.platform == "instagram" and not settings.public_media_base:
        raise ApiError(
            409,
            "PUBLIC_URL_REQUIRED",
            "Instagram publishing needs REELFORGE_PUBLIC_MEDIA_BASE (a public "
            "tunnel to this API) — see docs/publishing.md.",
        )

    accounts = (
        await db.execute(
            select(dbmod.SocialAccount).where(
                dbmod.SocialAccount.platform == body.platform
            )
        )
    ).scalars().all()
    if not accounts:
        raise ApiError(
            409,
            "SOCIAL_NOT_CONNECTED",
            f"No {body.platform} account connected — connect one first.",
        )
    if body.account_id is not None:
        account = next((a for a in accounts if a.id == body.account_id), None)
        if account is None:
            raise ApiError(
                404, "ACCOUNT_NOT_FOUND", f"account {body.account_id} is not connected"
            )
    elif len(accounts) == 1:
        account = accounts[0]
    else:
        raise ApiError(
            409,
            "CHANNEL_REQUIRED",
            f"Multiple {body.platform} accounts are connected — pass account_id to pick one.",
            channels=[
                {"account_id": a.id, "display_name": a.display_name}
                for a in accounts
            ],
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
        platform=body.platform,
        account_id=account.id,
        channel_title=account.display_name,
        # Instagram: Meta fetches the video from our public media route,
        # guarded by this single-use random token.
        public_token=(
            secrets.token_urlsafe(24) if body.platform == "instagram" else None
        ),
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


# ---- public media (Instagram fetch) ----------------------------------------


@router.get("/public/media/{token}")
async def public_media(
    token: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Serve one export to Meta's fetchers during an Instagram publish.

    Unauthenticated by design — the random per-publication token is the
    credential. Only valid while the owning publication is in flight, so the
    tunnel exposes exactly one file for the duration of one publish.
    """
    pub = (
        await db.execute(
            select(dbmod.Publication).where(
                dbmod.Publication.public_token == token,
                dbmod.Publication.status.in_(("queued", "uploading")),
            )
        )
    ).scalar_one_or_none()
    if pub is None:
        raise ApiError(404, "NOT_FOUND", "no active publication for this token")
    reel = await db.get(dbmod.Reel, pub.reel_id)
    if reel is None:
        raise ApiError(404, "NOT_FOUND", "reel gone")
    path = (
        settings.data_dir / "outputs" / reel.asset_id / pub.reel_id / f"{pub.preset_id}.mp4"
    )
    if not path.exists():
        raise ApiError(404, "NOT_FOUND", "export file missing")
    return await stream_file_with_range(path, request, media_type="video/mp4")
