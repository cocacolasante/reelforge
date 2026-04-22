"""Tests for the /analyze/estimate, /select/estimate, /usage endpoints."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


async def _upload_tiny(api_client, path: Path) -> tuple[str, str]:
    r = await api_client.post("/api/v1/projects", json={"name": "cost"})
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
    return pid, r.json()["id"]


@pytest.fixture(scope="module")
def tiny_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("cost") / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.mark.asyncio
async def test_analyze_estimate_returns_positive_cost(api_client, tiny_mp4: Path) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    r = await api_client.post(f"/api/v1/assets/{asset_id}/analyze/estimate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pricing_as_of"]
    assert body["scene_count_estimate"] >= 1
    assert body["estimated_cost_usd"] > 0
    assert body["breakdown"][0]["stage"] == "semantics"


@pytest.mark.asyncio
async def test_analyze_estimate_nonexistent_asset_404(api_client) -> None:
    r = await api_client.post("/api/v1/assets/nope/analyze/estimate", json={})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_select_estimate_requires_analysis(api_client, tiny_mp4: Path) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    r = await api_client.post(f"/api/v1/assets/{asset_id}/select/estimate", json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ANALYSIS_NOT_READY"


@pytest.mark.asyncio
async def test_select_estimate_happy_path(
    api_client, tiny_mp4: Path, isolated_data_dir: Path
) -> None:
    _pid, asset_id = await _upload_tiny(api_client, tiny_mp4)
    # Fabricate a minimal analysis.json so the estimate endpoint doesn't 409.
    from reelforge_core.analysis.pipeline import working_dir_for
    from reelforge_core.io_utils import write_json_atomic
    from reelforge_core.models import (
        AnalysisConfig,
        AnalysisReport,
        REELFORGE_VERSION,
        Scene,
        SceneSemantics,
    )

    wd = working_dir_for(asset_id)
    scenes = [
        Scene(
            index=i,
            start_sec=i * 10.0,
            end_sec=(i + 1) * 10.0,
            start_frame=0,
            end_frame=0,
            thumbnail_path=f"t{i}.jpg",
        )
        for i in range(8)
    ]
    sems = [
        SceneSemantics(
            scene_index=i,
            summary="s",
            tags=["a", "b", "c"],
            mood="neutral",
            has_speech=False,
            visual_energy="medium",
        )
        for i in range(8)
    ]
    report = AnalysisReport(
        asset_id=asset_id,
        source_path="/x.mp4",
        duration=80.0,
        width=640,
        height=360,
        fps=25.0,
        has_audio=False,
        config=AnalysisConfig(),
        scenes=scenes,
        transcript=None,
        loudness=[],
        semantics=sems,
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=0.0,
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage={},
    )
    write_json_atomic(wd / "analysis.json", json.loads(report.model_dump_json()))

    r = await api_client.post(f"/api/v1/assets/{asset_id}/select/estimate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidate_count"] > 0
    assert body["estimated_cost_usd"] > 0
    assert body["breakdown"][0]["stage"] == "ranking"


@pytest.mark.asyncio
async def test_usage_endpoints_empty(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "usage"})
    pid = r.json()["id"]
    r = await api_client.get(f"/api/v1/projects/{pid}/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    assert body["estimated_total_cost_usd"] == 0.0

    r = await api_client.get("/api/v1/usage")
    assert r.status_code == 200
    assert "total_calls" in r.json()


@pytest.mark.asyncio
async def test_usage_returns_recorded_rows(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "usage-real"})
    pid = r.json()["id"]
    # Inject a usage row directly (worker would normally do this).
    from reelforge_core.usage import record_anthropic_usage

    await record_anthropic_usage(
        job_id="job-fake",
        model="claude-haiku-4-5",
        input_tokens=1000,
        output_tokens=200,
        project_id=pid,
    )
    r = await api_client.get(f"/api/v1/projects/{pid}/usage")
    body = r.json()
    assert body["total_input_tokens"] == 1000
    assert body["total_output_tokens"] == 200
    assert body["estimated_total_cost_usd"] > 0
