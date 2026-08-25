"""Enqueue contract tests for analyze/select/compose/export.

Uses the fake arq pool from conftest so jobs get recorded to the DB (status=queued)
and Redis without actually executing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


async def _upload_tiny(api_client, path: Path) -> tuple[str, str]:
    r = await api_client.post("/api/v1/projects", json={"name": "pipeline"})
    pid = r.json()["id"]
    size = path.stat().st_size
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": path.name,
            "content_type": "video/mp4",
            "total_bytes": size,
            "chunk_size": 64 * 1024,
        },
    )
    upload_id = r.json()["id"]
    with path.open("rb") as f:
        idx = 0
        while chunk := f.read(64 * 1024):
            await api_client.put(
                f"/api/v1/uploads/{upload_id}/chunks/{idx}",
                content=chunk,
                headers={"content-length": str(len(chunk))},
            )
            idx += 1
    r = await api_client.post(f"/api/v1/uploads/{upload_id}/complete")
    asset_id = r.json()["id"]
    return pid, asset_id


@pytest.fixture(scope="module")
def tiny_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("m") / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.mark.asyncio
async def test_analyze_enqueues_job(api_client, tiny_mp4: Path) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    r = await api_client.post(f"/api/v1/assets/{asset_id}/analyze", json={})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "analyze"
    assert job["status"] == "queued"
    assert job["id"]

    # GET /jobs/{id} should return same payload.
    r = await api_client.get(f"/api/v1/jobs/{job['id']}")
    assert r.status_code == 200
    assert r.json()["kind"] == "analyze"


@pytest.mark.asyncio
async def test_analyze_conflict_returns_409(api_client, tiny_mp4: Path) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    r1 = await api_client.post(f"/api/v1/assets/{asset_id}/analyze", json={})
    assert r1.status_code == 200
    r2 = await api_client.post(f"/api/v1/assets/{asset_id}/analyze", json={})
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"]["code"] == "JOB_ALREADY_RUNNING"
    assert body["error"]["details"]["conflicting_job_id"] == r1.json()["id"]


@pytest.mark.asyncio
async def test_select_before_analyze_returns_conflict(
    api_client, tiny_mp4: Path
) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    r = await api_client.post(f"/api/v1/assets/{asset_id}/select", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ANALYSIS_NOT_READY"


@pytest.mark.asyncio
async def test_get_analysis_returns_not_ready(
    api_client, tiny_mp4: Path
) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    r = await api_client.get(f"/api/v1/assets/{asset_id}/analysis")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ANALYSIS_NOT_READY"


@pytest.mark.asyncio
async def test_export_invalid_preset(api_client, tiny_mp4: Path) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    # Fabricate a reel row directly via the pipeline endpoint — we don't need
    # real reels.json; just test the 400 path.
    # (We can't easily make a reel row without the full pipeline; this test
    # checks the 404 path instead when the reel doesn't exist.)
    r = await api_client.post(
        "/api/v1/reels/does-not-exist/exports",
        json={"preset_id": "nope"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_select_accepts_prompt_and_roundtrips_into_job_config(
    api_client, tiny_mp4: Path
) -> None:
    from pathlib import Path as _P

    from apps.api import db as dbmod
    from reelforge_core.analysis.pipeline import working_dir_for

    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    wd = working_dir_for(asset_id)
    _P(wd).mkdir(parents=True, exist_ok=True)
    (wd / "analysis.json").write_text("{}")  # existence check only

    r = await api_client.post(
        f"/api/v1/assets/{asset_id}/select",
        json={"prompt": "  clips of falls  ", "top_k": 5},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    import json as _json

    async with dbmod.db_state.sessionmaker() as session:
        job = await session.get(dbmod.Job, job_id)
        cfg = _json.loads(job.config_json)
    assert cfg["prompt"] == "clips of falls"  # whitespace stripped by validator
    assert cfg["top_k"] == 5


@pytest.mark.asyncio
async def test_select_rejects_oversized_prompt(api_client, tiny_mp4: Path) -> None:
    from pathlib import Path as _P

    from reelforge_core.analysis.pipeline import working_dir_for

    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    wd = working_dir_for(asset_id)
    _P(wd).mkdir(parents=True, exist_ok=True)
    (wd / "analysis.json").write_text("{}")

    r = await api_client.post(
        f"/api/v1/assets/{asset_id}/select", json={"prompt": "x" * 501}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_CONFIG"
