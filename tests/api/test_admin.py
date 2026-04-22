"""Admin router: disk usage, cleanup, cache purge, presets, batch compose."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_global_disk_usage(api_client, isolated_data_dir: Path) -> None:
    r = await api_client.get("/api/v1/disk_usage")
    assert r.status_code == 200
    body = r.json()
    for key in ("working_bytes", "outputs_bytes", "uploads_bytes", "cache_bytes"):
        assert key in body
        assert body[key] >= 0
    assert set(body["cache_breakdown"].keys()) == {"clip", "music", "caption_preview"}


@pytest.mark.asyncio
async def test_project_disk_usage_404_on_missing(api_client) -> None:
    r = await api_client.get("/api/v1/projects/does-not-exist/disk_usage")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_project_disk_usage_empty_breakdown(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "du"})
    pid = r.json()["id"]
    r = await api_client.get(f"/api/v1/projects/{pid}/disk_usage")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid
    assert body["breakdown"] == []
    assert body["total_bytes"] == 0


@pytest.mark.asyncio
async def test_cleanup_safe_mode_no_op_when_empty(
    api_client, isolated_data_dir: Path
) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "cleanup"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/cleanup", json={"mode": "safe"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "safe"
    assert body["bytes_freed"] == 0


@pytest.mark.asyncio
async def test_cleanup_working_mode_deletes(
    api_client, isolated_data_dir: Path
) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "cleanup-w"})
    pid = r.json()["id"]
    # Add an asset row with a pretend working dir that has content.
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        asset = dbmod.Asset(
            id="a" * 64,
            project_id=pid,
            path=str(isolated_data_dir / "uploads" / "a.mp4"),
            original_filename="a.mp4",
            duration_sec=1,
            width=100,
            height=100,
            fps=25,
            has_audio=False,
            size_bytes=1,
            probe_json="{}",
        )
        session.add(asset)
        await session.commit()
    wd = isolated_data_dir / "working" / ("a" * 64)
    wd.mkdir(parents=True)
    (wd / "data.txt").write_bytes(b"x" * 500)
    assert (wd / "data.txt").exists()

    r = await api_client.post(
        f"/api/v1/projects/{pid}/cleanup", json={"mode": "working"}
    )
    assert r.status_code == 200
    assert r.json()["bytes_freed"] >= 500
    assert not wd.exists()


@pytest.mark.asyncio
async def test_cache_purge(api_client, isolated_data_dir: Path) -> None:
    from reelforge_core import cache

    # Drop any cached thread-local so the new data_dir is picked up.
    if hasattr(cache._LOCAL, "conn"):
        try:
            cache._LOCAL.conn.close()
        except Exception:
            pass
        del cache._LOCAL.conn

    key = cache.compute_key("clip", {"k": 1})
    p = cache.path_for("clip", key, "mp4")
    p.write_bytes(b"x" * 200)
    cache.register(key, "clip", p)

    r = await api_client.post("/api/v1/cache/purge?kind=clip")
    assert r.status_code == 200
    assert r.json()["bytes_freed"] >= 200


@pytest.mark.asyncio
async def test_cache_purge_rejects_unknown_kind(api_client) -> None:
    r = await api_client.post("/api/v1/cache/purge?kind=bogus")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CONFIG"


@pytest.mark.asyncio
async def test_preset_crud(api_client) -> None:
    cfg = {"aspect": "1:1", "target_fps": 30}
    r = await api_client.post(
        "/api/v1/compose_presets",
        json={"name": "sq-1", "scope": "global", "config": cfg},
    )
    assert r.status_code == 201
    preset_id = r.json()["id"]

    r = await api_client.get("/api/v1/compose_presets")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    assert preset_id in ids

    r = await api_client.delete(f"/api/v1/compose_presets/{preset_id}")
    assert r.status_code == 204

    r = await api_client.get("/api/v1/compose_presets")
    ids = [p["id"] for p in r.json()]
    assert preset_id not in ids


@pytest.mark.asyncio
async def test_preset_rejects_invalid_config(api_client) -> None:
    r = await api_client.post(
        "/api/v1/compose_presets",
        json={"name": "bad", "scope": "global", "config": {"aspect": "nonsense"}},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CONFIG"


@pytest.mark.asyncio
async def test_batch_compose_asset_missing_returns_404(api_client) -> None:
    r = await api_client.post(
        "/api/v1/assets/nonexistent/compose_batch",
        json={"reel_ids": [], "config": {}},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_batch_compose_enqueues_jobs_and_reports_skipped(
    api_client, isolated_data_dir: Path
) -> None:
    # Create a project + asset + two reel rows.
    r = await api_client.post("/api/v1/projects", json={"name": "batch"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        aid = "b" * 64
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=60,
                width=1920,
                height=1080,
                fps=30,
                has_audio=True,
                size_bytes=1,
                probe_json="{}",
            )
        )
        session.add(
            dbmod.Reel(
                id="r1rr1rr1",
                project_id=pid,
                asset_id=aid,
                rank=1,
                title="t",
                hook="h",
                justification="j",
                start_sec=0,
                end_sec=30,
                duration_sec=30,
                overall_score=70,
                suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
            )
        )
        await session.commit()

    r = await api_client.post(
        f"/api/v1/assets/{aid}/compose_batch",
        json={"reel_ids": ["r1rr1rr1", "does-not-exist"], "config": {}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["job_ids"]) == 1
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason"] == "REEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_batch_compose_invalid_config(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "batch-bad"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    aid = "c" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=60,
                width=1920,
                height=1080,
                fps=30,
                has_audio=True,
                size_bytes=1,
                probe_json="{}",
            )
        )
        await session.commit()
    r = await api_client.post(
        f"/api/v1/assets/{aid}/compose_batch",
        json={"reel_ids": [], "config": {"aspect": "lol"}},
    )
    assert r.status_code == 400
