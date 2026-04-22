"""Job enqueue helpers with conflict detection."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from arq.connections import ArqRedis
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.schemas.errors import ApiError

log = logging.getLogger(__name__)


async def enqueue_job(
    db: AsyncSession,
    arq_pool: ArqRedis,
    *,
    project_id: Optional[str],
    kind: str,
    function_name: str,
    function_args: list[Any],
    function_kwargs: dict[str, Any] | None = None,
    config: BaseModel | None = None,
    conflict_filter=None,  # SQLAlchemy clause
    asset_id: Optional[str] = None,
    reel_id: Optional[str] = None,
    preset_id: Optional[str] = None,
) -> dbmod.Job:
    """Create the Job row first, then enqueue to arq with `_job_id=<row id>`.

    If `conflict_filter` matches any queued/running Job row, raise
    JOB_ALREADY_RUNNING without enqueuing.
    """
    function_kwargs = function_kwargs or {}

    if conflict_filter is not None:
        existing = (
            await db.execute(
                select(dbmod.Job).where(
                    and_(
                        conflict_filter,
                        dbmod.Job.status.in_(("queued", "running")),
                    )
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ApiError(
                409,
                "JOB_ALREADY_RUNNING",
                f"a {kind} job is already queued or running for this target",
                conflicting_job_id=existing.id,
            )

    row = dbmod.Job(
        kind=kind,
        project_id=project_id,
        asset_id=asset_id,
        reel_id=reel_id,
        preset_id=preset_id,
        status="queued",
        config_json=config.model_dump_json() if config is not None else None,
    )
    db.add(row)
    await db.flush()

    arq_job = await arq_pool.enqueue_job(
        function_name,
        *function_args,
        _job_id=row.id,
        **function_kwargs,
    )
    row.arq_job_id = arq_job.job_id if arq_job else row.id
    await db.commit()
    return row


async def get_job(db: AsyncSession, job_id: str) -> dbmod.Job:
    row = await db.get(dbmod.Job, job_id)
    if row is None:
        raise ApiError(404, "JOB_NOT_FOUND", f"job {job_id} not found")
    return row


async def job_with_live_progress(
    db: AsyncSession, redis, job_id: str
) -> dict[str, Any]:
    row = await get_job(db, job_id)
    progress = row.progress
    stage = row.stage
    message = row.message
    # If the job is still in-flight, let Redis override with fresher values.
    if row.status in ("queued", "running"):
        try:
            live = await redis.hgetall(f"job:{job_id}:progress")
        except Exception:
            live = {}
        if live:
            progress = float(live.get("overall", progress) or progress)
            stage = live.get("stage", stage) or stage
            message = live.get("message", message) or message

    logs: list[dict[str, Any]] = []
    try:
        logs = json.loads(row.logs_json or "[]")[-20:]
    except Exception:
        pass
    result = None
    if row.result_json:
        try:
            result = json.loads(row.result_json)
        except Exception:
            result = None
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "progress": float(progress or 0),
        "stage": stage,
        "message": message,
        "logs": logs,
        "result": result,
        "error": row.error_message,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }
