"""Reel detail + trim PATCH endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest


async def _project_asset_reel(api_client) -> tuple[str, str, str]:
    r = await api_client.post("/api/v1/projects", json={"name": "trim-test"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    aid = "d" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=120,
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
                id="reel-trim-1",
                project_id=pid,
                asset_id=aid,
                rank=1,
                title="trim me",
                hook="h",
                justification="j",
                start_sec=0.0,
                end_sec=60.0,
                duration_sec=60.0,
                overall_score=80,
                suggested_mood="neutral",
                scene_indices_json="[0,1,2]",
                scores_json='{"narrative_coherence":80,"hook_strength":80,"emotional_payoff":80,"standalone_clarity":80}',
            )
        )
        await session.commit()
    return pid, aid, "reel-trim-1"


@pytest.mark.asyncio
async def test_get_reel_returns_shape(api_client) -> None:
    _pid, _aid, rid = await _project_asset_reel(api_client)
    r = await api_client.get(f"/api/v1/reels/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rid
    assert body["mezzanine_ready"] is False
    assert body["scene_indices"] == [0, 1, 2]


@pytest.mark.asyncio
async def test_get_reel_404(api_client) -> None:
    r = await api_client.get("/api/v1/reels/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_patch_trim_valid_bounds(api_client) -> None:
    _pid, _aid, rid = await _project_asset_reel(api_client)
    r = await api_client.patch(
        f"/api/v1/reels/{rid}/trim",
        json={"trim_start_offset_sec": 0.5, "trim_end_offset_sec": 0.5},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_patch_trim_rejects_below_minimum_duration(api_client) -> None:
    pid, aid, _rid = await _project_asset_reel(api_client)
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Reel(
                id="reel-short",
                project_id=pid,
                asset_id=aid,
                rank=2,
                title="x",
                hook="x",
                justification="j",
                start_sec=0.0,
                end_sec=26.0,
                duration_sec=26.0,
                overall_score=70,
                suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
            )
        )
        await session.commit()
    r = await api_client.patch(
        "/api/v1/reels/reel-short/trim",
        json={"trim_start_offset_sec": 2.0, "trim_end_offset_sec": 0.0},
    )
    # 26 - 2 = 24, below the 25s floor → 400
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CONFIG"


@pytest.mark.asyncio
async def test_patch_trim_rejects_out_of_range(api_client) -> None:
    _pid, _aid, rid = await _project_asset_reel(api_client)
    r = await api_client.patch(
        f"/api/v1/reels/{rid}/trim",
        json={"trim_start_offset_sec": 10.0},
    )
    assert r.status_code == 422  # Pydantic validation error on field bounds


@pytest.mark.asyncio
async def test_patch_trim_invalidates_mezzanine(
    api_client, isolated_data_dir: Path
) -> None:
    _pid, aid, rid = await _project_asset_reel(api_client)
    # Fake an existing mezzanine on disk
    mezz = isolated_data_dir / "working" / aid / "reels" / rid / "mezzanine.mp4"
    mezz.parent.mkdir(parents=True, exist_ok=True)
    mezz.write_bytes(b"fake")
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, rid)
        assert row is not None
        row.mezzanine_path = str(mezz)
        await session.commit()

    r = await api_client.patch(
        f"/api/v1/reels/{rid}/trim", json={"trim_start_offset_sec": 0.2}
    )
    assert r.status_code == 200
    assert r.json()["mezzanine_ready"] is False
    assert not mezz.exists()


@pytest.mark.asyncio
async def test_get_reel_surfaces_prompt_relevance(api_client) -> None:
    pid, aid, reel_id = await _project_asset_reel(api_client)
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, reel_id)
        row.prompt_relevance = 88
        await session.commit()

    r = await api_client.get(f"/api/v1/reels/{reel_id}")
    assert r.status_code == 200
    assert r.json()["prompt_relevance"] == 88


@pytest.mark.asyncio
async def test_compose_plan_serves_smart_picks(api_client) -> None:
    pid, aid, reel_id = await _project_asset_reel(api_client)
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, reel_id)
        row.suggested_mood = "energetic"
        await session.commit()

    r = await api_client.get(f"/api/v1/reels/{reel_id}/compose_plan")
    assert r.status_code == 200
    body = r.json()
    assert body["mood"] == "energetic"
    assert body["transition"] == "slideleft"
    assert body["lut"] == "vivid"
    assert body["music"] == "auto-match"
    # Edit Quality v1: the grammar preview.
    assert body["style"] == "classic"  # no ranker classification on this row
    assert body["style_source"] == "fallback"
    assert isinstance(body["style_description"], str) and body["style_description"]

    r = await api_client.get("/api/v1/reels/nope/compose_plan")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_reel_surfaces_v2_source_and_opening(api_client) -> None:
    pid, aid, reel_id = await _project_asset_reel(api_client)
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, reel_id)
        row.source = "moment"
        row.opening_description = "rider drops in"
        row.edit_style = "hype"
        await session.commit()

    r = await api_client.get(f"/api/v1/reels/{reel_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "moment"
    assert body["opening_description"] == "rider drops in"
    assert body["edit_style"] == "hype"
