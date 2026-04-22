"""FastAPI dependency providers."""

from __future__ import annotations

from typing import AsyncIterator

from arq.connections import ArqRedis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod


async def get_db() -> AsyncIterator[AsyncSession]:
    async with dbmod.db_state.sessionmaker() as session:
        yield session


def get_arq(request: Request) -> ArqRedis:
    return request.app.state.arq_pool


def get_redis(request: Request):
    return request.app.state.redis


def get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)
