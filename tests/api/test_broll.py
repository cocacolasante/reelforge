"""POST /reels/{id}/broll/suggest: enqueue contract and validation."""

from __future__ import annotations

import pytest

from tests.api.test_reel_edit import _seed


@pytest.mark.asyncio
async def test_suggest_enqueues_broll_job_for_current_timeline(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    from sqlalchemy import select

    from apps.api import db as dbmod

    timeline = {"shots": [{"kind": "video", "asset_id": vid, "in_ts": 0, "out_ts": 20}]}
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/broll/suggest", json={"timeline": timeline, "prompt": "  beach  "}
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "broll"
    async with dbmod.db_state.sessionmaker() as session:
        job = (await session.execute(
            select(dbmod.Job).where(dbmod.Job.kind == "broll")
        )).scalars().one()
    assert job.reel_id == reel_id

    # One suggestion job per reel at a time.
    again = await api_client.post(f"/api/v1/reels/{reel_id}/broll/suggest", json={})
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_suggest_rejects_foreign_assets_and_unknown_reels(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    bad = {"shots": [{"kind": "video", "asset_id": "z" * 64, "in_ts": 0, "out_ts": 5}]}
    r = await api_client.post(f"/api/v1/reels/{reel_id}/broll/suggest", json={"timeline": bad})
    assert r.status_code == 400
    r = await api_client.post("/api/v1/reels/nope/broll/suggest", json={})
    assert r.status_code == 404
