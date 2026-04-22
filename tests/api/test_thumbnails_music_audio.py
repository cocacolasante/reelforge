"""Thumbnail endpoint + music audio streaming endpoint error paths."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_thumbnail_missing_asset(api_client) -> None:
    r = await api_client.get("/api/v1/assets/does-not-exist/thumbnails/0")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_thumbnail_missing_file(api_client, isolated_data_dir: Path) -> None:
    # Seed an asset with no working dir; thumbnail endpoint should 404
    # cleanly rather than crashing.
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "thumb"})
    pid = r.json()["id"]
    aid = "t" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=1,
                width=100,
                height=100,
                fps=25,
                has_audio=False,
                size_bytes=1,
                probe_json="{}",
            )
        )
        await session.commit()
    r = await api_client.get(f"/api/v1/assets/{aid}/thumbnails/0")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ANALYSIS_NOT_READY"


@pytest.mark.asyncio
async def test_thumbnail_serves_jpeg(api_client, isolated_data_dir: Path) -> None:
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "thumb-real"})
    pid = r.json()["id"]
    aid = "u" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=1,
                width=100,
                height=100,
                fps=25,
                has_audio=False,
                size_bytes=1,
                probe_json="{}",
            )
        )
        await session.commit()
    thumb = isolated_data_dir / "working" / aid / "thumbs" / "scene_0000.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    # Write a minimal valid-looking JPEG (just bytes; we only check content-type).
    thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    r = await api_client.get(f"/api/v1/assets/{aid}/thumbnails/0")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/jpeg")


@pytest.mark.asyncio
async def test_music_audio_missing_track(api_client) -> None:
    r = await api_client.get("/api/v1/music/does-not-exist/audio")
    assert r.status_code == 404
