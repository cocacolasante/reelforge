"""AI Mix CP2: create/list endpoints, synthetic row shape, guards."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.analysis.pipeline import working_dir_for


async def _seed_project_with_clips(
    api_client, isolated_data_dir: Path, n_analyzed: int = 2
) -> tuple[str, list[str]]:
    r = await api_client.post("/api/v1/projects", json={"name": "mix-test"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    aids = []
    async with dbmod.db_state.sessionmaker() as session:
        for i in range(n_analyzed):
            aid = chr(ord("e") + i) * 64
            src = isolated_data_dir / f"src{i}.mp4"
            src.write_bytes(b"fake video bytes")
            session.add(
                dbmod.Asset(
                    id=aid,
                    project_id=pid,
                    path=str(src),
                    original_filename=f"run{i}.mp4",
                    duration_sec=120 + i * 30,
                    width=1920,
                    height=1080,
                    fps=30,
                    has_audio=True,
                    size_bytes=1,
                    probe_json="{}",
                )
            )
            wd = working_dir_for(aid)
            (wd / "analysis.json").write_text("{}")  # existence check only
            (wd / "probe.json").write_text(json.dumps({"path": str(src)}))
            aids.append(aid)
        await session.commit()
    return pid, aids


@pytest.mark.asyncio
async def test_create_mix_requires_two_analyzed_clips(
    api_client, isolated_data_dir: Path
) -> None:
    pid, _ = await _seed_project_with_clips(api_client, isolated_data_dir, n_analyzed=1)
    r = await api_client.post(f"/api/v1/projects/{pid}/mixes", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NOT_ENOUGH_CLIPS"

    r = await api_client.post("/api/v1/projects/nope/mixes", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_mix_validates_body(api_client, isolated_data_dir: Path) -> None:
    pid, _ = await _seed_project_with_clips(api_client, isolated_data_dir)
    r = await api_client.post(
        f"/api/v1/projects/{pid}/mixes", json={"target_duration_sec": 5}
    )
    assert r.status_code == 422
    r = await api_client.post(
        f"/api/v1/projects/{pid}/mixes", json={"style": "vaporwave"}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_mix_mints_row_and_job(api_client, isolated_data_dir: Path) -> None:
    pid, aids = await _seed_project_with_clips(api_client, isolated_data_dir)
    r = await api_client.post(
        f"/api/v1/projects/{pid}/mixes",
        json={"target_duration_sec": 60, "prompt": "  jumps only  ", "style": "hype"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "compose" and body["status"] == "queued"

    listing = (await api_client.get(f"/api/v1/projects/{pid}/mixes")).json()["mixes"]
    assert len(listing) == 1
    mix = listing[0]
    assert mix["id"].startswith("mix-")
    assert mix["mezzanine_ready"] is False

    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, mix["id"])
        assert row.asset_id == aids[1]  # the longer clip is primary
        assert row.scene_indices_json == "[]"
        assert set(json.loads(row.scores_json)) == {
            "narrative_coherence",
            "hook_strength",
            "emotional_payoff",
            "standalone_clarity",
        }
        assert row.child_reel_ids_json is None  # never in the montage list
        assert row.mezzanine_path is None  # no pre-write

    # Mixes don't leak into the montage list, and vice versa.
    montages = (await api_client.get(f"/api/v1/projects/{pid}/montages")).json()
    assert montages["montages"] == []

    # The reel detail endpoint serves the synthetic row.
    detail = (await api_client.get(f"/api/v1/reels/{mix['id']}")).json()
    assert detail["scene_indices"] == [] and detail["mezzanine_ready"] is False


@pytest.mark.asyncio
async def test_mix_guards_trim_and_edit_reset(api_client, isolated_data_dir: Path) -> None:
    pid, aids = await _seed_project_with_clips(api_client, isolated_data_dir)
    await api_client.post(f"/api/v1/projects/{pid}/mixes", json={})
    mix_id = (await api_client.get(f"/api/v1/projects/{pid}/mixes")).json()["mixes"][0]["id"]

    r = await api_client.patch(
        f"/api/v1/reels/{mix_id}/trim", json={"trim_start_offset_sec": 0.5}
    )
    assert r.status_code == 400
    r = await api_client.delete(f"/api/v1/reels/{mix_id}/edit")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_mix_compose_endpoint_accepts_synthetic_row(
    api_client, isolated_data_dir: Path
) -> None:
    """POST /reels/{mix}/compose must enqueue (the stub keeps the worker from
    needing a reels.json entry) once the mix has a timeline."""
    pid, aids = await _seed_project_with_clips(api_client, isolated_data_dir)
    await api_client.post(f"/api/v1/projects/{pid}/mixes", json={})
    mix_id = (await api_client.get(f"/api/v1/projects/{pid}/mixes")).json()["mixes"][0]["id"]

    from apps.api import db as dbmod
    from reelforge_core.models import ReelTimeline, TimelineShot

    timeline = ReelTimeline(
        shots=[
            TimelineShot(kind="video", asset_id=aids[0], in_ts=0.0, out_ts=5.0),
            TimelineShot(kind="video", asset_id=aids[1], in_ts=10.0, out_ts=15.0),
        ]
    )
    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, mix_id)
        row.edit_json = timeline.model_dump_json()
        await session.commit()

    r = await api_client.post(f"/api/v1/reels/{mix_id}/compose", json={})
    # The create-mix job is still "queued" on the same reel_id -> the compose
    # conflict filter correctly reports 409; that IS the contract.
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_ALREADY_RUNNING"

    # Once the original job is done, compose enqueues fine.
    async with dbmod.db_state.sessionmaker() as session:
        from sqlalchemy import select

        jobs = (
            (await session.execute(select(dbmod.Job).where(dbmod.Job.reel_id == mix_id)))
            .scalars()
            .all()
        )
        for j in jobs:
            j.status = "done"
        await session.commit()
    r = await api_client.post(f"/api/v1/reels/{mix_id}/compose", json={})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "compose"
