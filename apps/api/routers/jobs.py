"""Jobs: detail, listing, SSE progress stream."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.deps import get_db, get_redis
from apps.api.schemas.common import JobList, JobOut
from apps.api.schemas.errors import ApiError
from apps.api.services.jobs import job_with_live_progress

log = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_status(
    job_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> JobOut:
    data = await job_with_live_progress(db, request.app.state.redis, job_id)
    return JobOut(**data)


@router.get("/projects/{project_id}/jobs", response_model=JobList)
async def list_project_jobs(
    project_id: str,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
) -> JobList:
    stmt = (
        select(dbmod.Job)
        .where(dbmod.Job.project_id == project_id)
        .order_by(dbmod.Job.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(dbmod.Job.status == status)
    if kind:
        stmt = stmt.where(dbmod.Job.kind == kind)
    rows = (await db.execute(stmt)).scalars().all()
    out: list[JobOut] = []
    for r in rows:
        data = await job_with_live_progress(db, request.app.state.redis, r.id)
        out.append(JobOut(**data))
    return JobList(jobs=out)


def _sse_format(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode("utf-8")


async def _progress_generator(
    job_id: str, db_session_factory, redis, request: Request
):
    last = None
    while True:
        if await request.is_disconnected():
            return
        async with db_session_factory() as session:
            try:
                data = await job_with_live_progress(session, redis, job_id)
            except ApiError:
                yield _sse_format(
                    "failed",
                    {"status": "failed", "error": f"job {job_id} not found"},
                )
                return
        payload = {
            "status": data["status"],
            "progress": data["progress"],
            "stage": data["stage"],
            "message": data["message"],
        }
        if payload != last:
            yield _sse_format("progress", payload)
            last = payload
        if data["status"] in ("done", "failed"):
            final_event = "done" if data["status"] == "done" else "failed"
            yield _sse_format(
                final_event,
                {
                    "status": data["status"],
                    "result": data["result"],
                    "error": data["error"],
                },
            )
            return
        await asyncio.sleep(0.5)


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(
    job_id: str, request: Request, db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    # Probe the row once to produce a clean 404 before opening the stream.
    row = await db.get(dbmod.Job, job_id)
    if row is None:
        raise ApiError(404, "JOB_NOT_FOUND", f"job {job_id} not found")

    async def _gen():
        async for chunk in _progress_generator(
            job_id,
            dbmod.db_state.sessionmaker,
            request.app.state.redis,
            request,
        ):
            yield chunk

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable proxy buffering
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)
