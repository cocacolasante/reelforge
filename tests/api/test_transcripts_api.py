"""Transcript override endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.io_utils import write_json_atomic


async def _seed_asset(api_client, duration=30.0) -> tuple[str, str]:
    r = await api_client.post("/api/v1/projects", json={"name": "transcript"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    aid = "e" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=duration,
                width=1920,
                height=1080,
                fps=30,
                has_audio=True,
                size_bytes=1,
                probe_json="{}",
            )
        )
        await session.commit()
    return pid, aid


@pytest.mark.asyncio
async def test_get_transcript_not_ready(api_client) -> None:
    _pid, aid = await _seed_asset(api_client)
    r = await api_client.get(f"/api/v1/assets/{aid}/transcript")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ANALYSIS_NOT_READY"


@pytest.mark.asyncio
async def test_get_transcript_from_whisper(api_client, isolated_data_dir: Path) -> None:
    _pid, aid = await _seed_asset(api_client)
    wd = working_dir_for(aid)
    wd.mkdir(parents=True, exist_ok=True)
    # Silent source fallback
    write_json_atomic(wd / "transcript.json", {"transcript": None})
    r = await api_client.get(f"/api/v1/assets/{aid}/transcript")
    assert r.status_code == 200
    assert r.json()["source"] == "whisper"
    assert r.json()["transcript"] is None


@pytest.mark.asyncio
async def test_put_transcript_saves_override(api_client, isolated_data_dir: Path) -> None:
    _pid, aid = await _seed_asset(api_client)
    transcript = {
        "language": "en",
        "language_probability": 0.99,
        "duration": 2.0,
        "segments": [
            {
                "start": 0.0,
                "end": 1.0,
                "text": " hi",
                "words": [
                    {"start": 0.1, "end": 0.3, "word": " hi", "probability": 0.9}
                ],
            }
        ],
    }
    r = await api_client.put(f"/api/v1/assets/{aid}/transcript", json=transcript)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "override"

    # GET now prefers the override
    r = await api_client.get(f"/api/v1/assets/{aid}/transcript")
    assert r.json()["source"] == "override"


@pytest.mark.asyncio
async def test_put_rejects_non_monotonic(api_client) -> None:
    _pid, aid = await _seed_asset(api_client)
    bad = {
        "language": "en",
        "language_probability": 0.99,
        "duration": 2.0,
        "segments": [
            {"start": 1.0, "end": 2.0, "text": "b", "words": [{"start": 1.0, "end": 1.2, "word": "b", "probability": 0.9}]},
            {"start": 0.0, "end": 0.5, "text": "a", "words": [{"start": 0.0, "end": 0.2, "word": "a", "probability": 0.9}]},
        ],
    }
    r = await api_client.put(f"/api/v1/assets/{aid}/transcript", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CONFIG"


@pytest.mark.asyncio
async def test_put_rejects_shape_error(api_client) -> None:
    _pid, aid = await _seed_asset(api_client)
    r = await api_client.put(f"/api/v1/assets/{aid}/transcript", json={"segments": []})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_override(api_client) -> None:
    _pid, aid = await _seed_asset(api_client)
    transcript = {
        "language": "en",
        "language_probability": 0.99,
        "duration": 1.0,
        "segments": [
            {"start": 0, "end": 1, "text": "x", "words": [{"start": 0, "end": 0.1, "word": "x", "probability": 1.0}]}
        ],
    }
    await api_client.put(f"/api/v1/assets/{aid}/transcript", json=transcript)
    r = await api_client.delete(f"/api/v1/assets/{aid}/transcript")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_missing_asset_404(api_client) -> None:
    r = await api_client.get("/api/v1/assets/does-not-exist/transcript")
    assert r.status_code == 404
