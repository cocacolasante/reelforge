"""Compose enqueue + exports router + error paths."""

from __future__ import annotations

from pathlib import Path

import pytest


async def _seed(
    api_client, isolated_data_dir: Path, *, with_mezz: bool = False
) -> tuple[str, str, str]:
    from apps.api import db as dbmod
    from reelforge_core.analysis.pipeline import working_dir_for

    r = await api_client.post("/api/v1/projects", json={"name": "c-e"})
    pid = r.json()["id"]
    aid = "h" * 64
    rid = "reel-c-e-1"
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
        session.add(
            dbmod.Reel(
                id=rid,
                project_id=pid,
                asset_id=aid,
                rank=1,
                title="x",
                hook="x",
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
    if with_mezz:
        mezz = working_dir_for(aid) / "reels" / rid / "mezzanine.mp4"
        mezz.parent.mkdir(parents=True, exist_ok=True)
        mezz.write_bytes(b"fake" * 256)
    return pid, aid, rid


@pytest.mark.asyncio
async def test_compose_enqueue_missing_reel(api_client) -> None:
    r = await api_client.post("/api/v1/reels/does-not-exist/compose", json={})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_compose_enqueue_happy(api_client, isolated_data_dir: Path) -> None:
    _pid, _aid, rid = await _seed(api_client, isolated_data_dir)
    r = await api_client.post(f"/api/v1/reels/{rid}/compose", json={"aspect": "1:1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "compose"
    assert body["status"] == "queued"


@pytest.mark.asyncio
async def test_compose_manifest_not_ready(api_client, isolated_data_dir: Path) -> None:
    _pid, _aid, rid = await _seed(api_client, isolated_data_dir)
    r = await api_client.get(f"/api/v1/reels/{rid}/compose")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "MEZZANINE_NOT_READY"


@pytest.mark.asyncio
async def test_export_enqueue_before_mezz_is_409(
    api_client, isolated_data_dir: Path
) -> None:
    _pid, _aid, rid = await _seed(api_client, isolated_data_dir, with_mezz=False)
    r = await api_client.post(
        f"/api/v1/reels/{rid}/exports", json={"preset_id": "mp4_h264_social"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "MEZZANINE_NOT_READY"


@pytest.mark.asyncio
async def test_export_enqueue_invalid_preset(api_client, isolated_data_dir: Path) -> None:
    _pid, _aid, rid = await _seed(api_client, isolated_data_dir, with_mezz=True)
    r = await api_client.post(
        f"/api/v1/reels/{rid}/exports", json={"preset_id": "nope"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PRESET"


@pytest.mark.asyncio
async def test_export_enqueue_happy_and_list(
    api_client, isolated_data_dir: Path
) -> None:
    _pid, _aid, rid = await _seed(api_client, isolated_data_dir, with_mezz=True)
    r = await api_client.post(
        f"/api/v1/reels/{rid}/exports", json={"preset_id": "mp4_h264_social"}
    )
    assert r.status_code == 200, r.text
    r = await api_client.get(f"/api/v1/reels/{rid}/exports")
    assert r.status_code == 200
    assert len(r.json()["exports"]) == 1
    assert r.json()["exports"][0]["preset_id"] == "mp4_h264_social"


@pytest.mark.asyncio
async def test_export_detail_not_found(api_client) -> None:
    r = await api_client.get("/api/v1/exports/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "EXPORT_NOT_FOUND"


@pytest.mark.asyncio
async def test_export_download_missing_file(
    api_client, isolated_data_dir: Path
) -> None:
    # Seed an export row with output_path pointing nowhere.
    _pid, _aid, rid = await _seed(api_client, isolated_data_dir, with_mezz=True)
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Export(
                id="ghost-export",
                reel_id=rid,
                preset_id="mp4_h264_social",
                output_path="/tmp/does-not-exist.mp4",
            )
        )
        await session.commit()
    r = await api_client.get("/api/v1/exports/ghost-export/download")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "EXPORT_NOT_FOUND"
