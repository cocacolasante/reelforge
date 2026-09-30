"""Agent access: mint, list and revoke the keys agents authenticate with.

These routes are for the dashboard (localhost), not for agents — nothing
here is reachable through an MCP tool, so a key can never mint another key
or revoke the one keeping a connector alive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db
from apps.api.schemas.errors import ApiError
from apps.api.services import api_keys
from apps.api.settings import settings

router = APIRouter(tags=["api-keys"])
log = logging.getLogger(__name__)


class KeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class KeyCreated(KeyOut):
    # The only time the token exists outside the agent that will hold it.
    token: str


class KeyList(BaseModel):
    keys: list[KeyOut]


class UploadLinkRequest(BaseModel):
    name: str = Field(default="", max_length=80)


class UploadLinkOut(BaseModel):
    project_id: str
    project_name: str
    upload_url: str
    expires_in_seconds: int
    reachable_publicly: bool


class AgentAccessInfo(BaseModel):
    mcp_url: str
    reachable_publicly: bool


def _out(row: dbmod.ApiKey) -> KeyOut:
    return KeyOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


@router.get("/api-keys/connection", response_model=AgentAccessInfo)
async def connection_info() -> AgentAccessInfo:
    """What to paste into the agent. `public_media_base` is the tunnel's
    hostname when one is running; localhost otherwise, which no phone can
    reach — so the UI can say that rather than handing over a dead URL."""
    base = (settings.public_media_base or settings.public_api_base).rstrip("/")
    return AgentAccessInfo(
        mcp_url=f"{base}/mcp",
        reachable_publicly=bool(settings.public_media_base),
    )


@router.post("/agent/upload-link", response_model=UploadLinkOut, status_code=201)
async def create_upload_link(
    body: UploadLinkRequest, db: AsyncSession = Depends(get_db)
) -> UploadLinkOut:
    """A project plus a signed link for putting footage into it.

    Called by the `start_upload` tool: an agent can't carry video, so this is
    how footage gets in. A fresh project per link keeps one shoot's clips
    together, which is what the cutting tools take as their unit.
    """
    from apps.api.routers.upload_link import UPLOAD_LINK_TTL_S, upload_url

    name = body.name.strip() or f"Footage {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"
    project = dbmod.Project(name=name)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    log.info("agent upload link for project %s (%s)", project.id, project.name)
    return UploadLinkOut(
        project_id=project.id,
        project_name=project.name,
        upload_url=upload_url(project.id),
        expires_in_seconds=UPLOAD_LINK_TTL_S,
        reachable_publicly=bool(settings.public_media_base),
    )


@router.get("/api-keys", response_model=KeyList)
async def list_keys(db: AsyncSession = Depends(get_db)) -> KeyList:
    rows = (
        await db.execute(select(dbmod.ApiKey).order_by(dbmod.ApiKey.created_at.desc()))
    ).scalars().all()
    return KeyList(keys=[_out(r) for r in rows])


@router.post("/api-keys", response_model=KeyCreated, status_code=201)
async def create_key(body: KeyCreate, db: AsyncSession = Depends(get_db)) -> KeyCreated:
    token, prefix, token_hash = api_keys.mint()
    row = dbmod.ApiKey(name=body.name.strip(), prefix=prefix, token_hash=token_hash)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info("minted api key %s (%s)", row.id, row.name)
    return KeyCreated(**_out(row).model_dump(), token=token)


@router.post("/api-keys/{key_id}/revoke", response_model=KeyOut)
async def revoke_key(key_id: str, db: AsyncSession = Depends(get_db)) -> KeyOut:
    row = await db.get(dbmod.ApiKey, key_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"no API key {key_id}")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return _out(row)
