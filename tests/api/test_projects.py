from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_and_list_projects(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "Proj A"})
    assert r.status_code == 201, r.text
    pa = r.json()
    assert pa["name"] == "Proj A"
    assert "id" in pa

    r = await api_client.post("/api/v1/projects", json={"name": "Proj B"})
    pb = r.json()

    r = await api_client.get("/api/v1/projects")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    ids = {p["id"] for p in body["projects"]}
    assert pa["id"] in ids and pb["id"] in ids


@pytest.mark.asyncio
async def test_project_not_found_returns_envelope(api_client) -> None:
    r = await api_client.get("/api/v1/projects/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "PROJECT_NOT_FOUND"
    assert "request_id" in body


@pytest.mark.asyncio
async def test_delete_project(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "to-delete"})
    pid = r.json()["id"]
    r = await api_client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    r = await api_client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_health_endpoint(api_client) -> None:
    r = await api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    for key in ("status", "ffmpeg", "redis", "anthropic_configured"):
        assert key in body


@pytest.mark.asyncio
async def test_ready_endpoint(api_client) -> None:
    r = await api_client.get("/ready")
    assert r.status_code == 200
    assert r.json()["ready"] is True


@pytest.mark.asyncio
async def test_request_id_header_returned(api_client) -> None:
    r = await api_client.get("/health")
    assert "x-request-id" in {k.lower() for k in r.headers.keys()}


@pytest.mark.asyncio
async def test_delete_asset_removes_rows_and_files(api_client) -> None:
    """Deleting a source clip drops its derived rows and its files."""
    import apps.api.settings as settings_mod
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "del-asset"})
    pid = r.json()["id"]
    aid = "f" * 64
    data_dir = settings_mod.settings.data_dir
    upload = data_dir / "uploads" / f"{aid}.mp4"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(b"x" * 64)
    working = data_dir / "working" / aid
    working.mkdir(parents=True, exist_ok=True)
    (working / "analysis.json").write_text("{}")
    outputs = data_dir / "outputs" / aid / "reel-del-1"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "mp4_h264_social.mp4").write_bytes(b"y" * 64)

    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path=str(upload),
                original_filename="big.mp4",
                duration_sec=266,
                width=3840,
                height=2160,
                fps=30,
                has_audio=True,
                size_bytes=2_000_000_000,
                probe_json="{}",
            )
        )
        session.add(
            dbmod.Reel(
                id="reel-del-1",
                project_id=pid,
                asset_id=aid,
                rank=1,
                title="t",
                hook="h",
                justification="j",
                start_sec=0.0,
                end_sec=30.0,
                duration_sec=30.0,
                overall_score=70,
                suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
            )
        )
        session.add(
            dbmod.Job(id="job-del-1", kind="analyze", project_id=pid, asset_id=aid, status="running")
        )
        await session.commit()
    # Separate commit: the export's FK needs its reel row to exist first.
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Export(id="exp-del-1", reel_id="reel-del-1", preset_id="mp4_h264_social")
        )
        await session.commit()

    resp = await api_client.delete(f"/api/v1/assets/{aid}")
    assert resp.status_code == 204, resp.text

    # Rows gone.
    listing = await api_client.get(f"/api/v1/projects/{pid}/assets")
    assert listing.json()["assets"] == []
    async with dbmod.db_state.sessionmaker() as session:
        assert await session.get(dbmod.Reel, "reel-del-1") is None
        assert await session.get(dbmod.Export, "exp-del-1") is None
        assert await session.get(dbmod.Job, "job-del-1") is None
    # Files gone.
    assert not upload.exists()
    assert not working.exists()
    assert not (data_dir / "outputs" / aid).exists()


@pytest.mark.asyncio
async def test_delete_asset_404_when_missing(api_client) -> None:
    r = await api_client.delete("/api/v1/assets/" + "0" * 64)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ASSET_NOT_FOUND"
