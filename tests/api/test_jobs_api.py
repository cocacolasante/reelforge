"""Jobs detail + project listing."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_job_not_found(api_client) -> None:
    r = await api_client.get("/api/v1/jobs/nonexistent")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_list_project_jobs_empty(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "jobs"})
    pid = r.json()["id"]
    r = await api_client.get(f"/api/v1/projects/{pid}/jobs")
    assert r.status_code == 200
    assert r.json()["jobs"] == []


@pytest.mark.asyncio
async def test_list_project_jobs_filters(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "jobs2"})
    pid = r.json()["id"]
    # Seed two job rows
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Job(
                id="ja",
                kind="analyze",
                status="done",
                project_id=pid,
                logs_json="[]",
                progress=1.0,
            )
        )
        session.add(
            dbmod.Job(
                id="jb",
                kind="compose",
                status="failed",
                project_id=pid,
                logs_json="[]",
                progress=0.5,
            )
        )
        await session.commit()

    r = await api_client.get(f"/api/v1/projects/{pid}/jobs")
    body = r.json()
    assert len(body["jobs"]) == 2

    r = await api_client.get(f"/api/v1/projects/{pid}/jobs?kind=analyze")
    assert len(r.json()["jobs"]) == 1

    r = await api_client.get(f"/api/v1/projects/{pid}/jobs?status=failed")
    assert len(r.json()["jobs"]) == 1


@pytest.mark.asyncio
async def test_sse_stream_missing_job_is_404(api_client) -> None:
    r = await api_client.get("/api/v1/jobs/nonexistent/stream")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_job_with_terminal_status_returns_shape(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "jobs-term"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Job(
                id="jc",
                kind="analyze",
                status="done",
                project_id=pid,
                result_json='{"ok":true}',
                logs_json='[{"ts":"2026-01-01T00:00:00+00:00","level":"INFO","msg":"hi"}]',
                progress=1.0,
                stage="done",
                message="done",
            )
        )
        await session.commit()
    r = await api_client.get("/api/v1/jobs/jc")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"] == {"ok": True}
    assert body["logs"][0]["msg"] == "hi"
