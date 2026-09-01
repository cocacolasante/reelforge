"""Project-level reel aggregation + montage create + list endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.io_utils import write_json_atomic


def _build_selection_on_disk(
    asset_id: str, project_id: str, n_reels: int, relevance: int | None = None
) -> None:
    from reelforge_core.models import (
        REELFORGE_VERSION,
        RankedReel,
        ReelScores,
        ReelSelection,
        SelectionConfig,
    )

    reels = [
        RankedReel(
            candidate_id=f"reel-{asset_id[:4]}-{i}",
            scene_indices=[i, i + 1],
            start_sec=float(i * 30),
            end_sec=float(i * 30 + 30),
            duration_sec=30.0,
            title=f"reel {i}",
            hook=f"hook {i}",
            justification="x",
            scores=ReelScores(
                narrative_coherence=70 + i,
                hook_strength=70 + i,
                emotional_payoff=70,
                standalone_clarity=70,
            ),
            overall=70 + i,
            rank=i + 1,
            suggested_mood="neutral",
            prompt_relevance=relevance,
            source="sentence",
            opening_description=f"literal opening {i}",
            edit_style="hype",
        )
        for i in range(n_reels)
    ]
    sel = ReelSelection(
        asset_id=asset_id,
        analysis_source="analysis.json",
        config=SelectionConfig(),
        candidates_generated=n_reels * 2,
        candidates_dropped_by_dedup=n_reels,
        reels=reels,
        anthropic_usage={},
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=0.0,
        reelforge_version=REELFORGE_VERSION,
    )
    write_json_atomic(working_dir_for(asset_id) / "reels.json", json.loads(sel.model_dump_json()))


@pytest.mark.asyncio
async def test_project_reels_aggregates_across_assets(
    api_client, isolated_data_dir: Path
) -> None:
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "agg"})
    pid = r.json()["id"]
    a1 = "m" * 64
    a2 = "n" * 64
    async with dbmod.db_state.sessionmaker() as session:
        for aid in (a1, a2):
            session.add(
                dbmod.Asset(
                    id=aid,
                    project_id=pid,
                    path=f"/tmp/{aid}.mp4",
                    original_filename=f"{aid}.mp4",
                    duration_sec=120,
                    width=1920,
                    height=1080,
                    fps=30,
                    has_audio=True,
                    size_bytes=1,
                    probe_json="{}",
                )
            )
        await session.commit()
    _build_selection_on_disk(a1, pid, 3)
    _build_selection_on_disk(a2, pid, 2)

    r = await api_client.get(f"/api/v1/projects/{pid}/reels")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asset_count"] == 2
    assert len(body["reels"]) == 5
    # Sorted by overall_score desc → project_rank numbered 1..5
    ranks = [reel["project_rank"] for reel in body["reels"]]
    assert ranks == [1, 2, 3, 4, 5]
    scores = [reel["overall_score"] for reel in body["reels"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_project_reels_empty_when_no_selection(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "empty"})
    pid = r.json()["id"]
    r = await api_client.get(f"/api/v1/projects/{pid}/reels")
    assert r.status_code == 200
    assert r.json()["reels"] == []


@pytest.mark.asyncio
async def test_project_reels_404_on_missing_project(api_client) -> None:
    r = await api_client.get("/api/v1/projects/does-not-exist/reels")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_montage_requires_min_one_reel(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "montage-empty"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/montages",
        json={"reel_ids": []},
    )
    # Pydantic min_length=1 → 422
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_montage_rejects_missing_reel(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "montage-miss"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/montages",
        json={"reel_ids": ["nonexistent"]},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_montage_rejects_reel_without_mezzanine(
    api_client, isolated_data_dir: Path
) -> None:
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "montage-nomezz"})
    pid = r.json()["id"]
    aid = "o" * 64
    rid = "reel-no-mezz-x"
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid, project_id=pid, path="/tmp/x.mp4",
                original_filename="x.mp4", duration_sec=60,
                width=1920, height=1080, fps=30, has_audio=True,
                size_bytes=1, probe_json="{}",
            )
        )
        session.add(
            dbmod.Reel(
                id=rid, project_id=pid, asset_id=aid, rank=1,
                title="t", hook="h", justification="j",
                start_sec=0, end_sec=30, duration_sec=30,
                overall_score=70, suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
                mezzanine_path=None,
            )
        )
        await session.commit()
    r = await api_client.post(
        f"/api/v1/projects/{pid}/montages",
        json={"reel_ids": [rid]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "MEZZANINE_NOT_READY"


@pytest.mark.asyncio
async def test_list_montages_empty(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "no-montages"})
    pid = r.json()["id"]
    r = await api_client.get(f"/api/v1/projects/{pid}/montages")
    assert r.status_code == 200
    assert r.json()["montages"] == []


@pytest.mark.asyncio
async def test_create_montage_enqueues_job(
    api_client, isolated_data_dir: Path
) -> None:
    """Happy path: two reels with mezzanines on disk → montage row created + job queued."""
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "montage-go"})
    pid = r.json()["id"]
    aid = "p" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid, project_id=pid, path="/tmp/x.mp4",
                original_filename="x.mp4", duration_sec=120,
                width=1920, height=1080, fps=30, has_audio=True,
                size_bytes=1, probe_json="{}",
            )
        )
        for i in range(2):
            mezz = working_dir_for(aid) / "reels" / f"chap-{i}" / "mezzanine.mp4"
            mezz.parent.mkdir(parents=True, exist_ok=True)
            mezz.write_bytes(b"x" * 4096)
            session.add(
                dbmod.Reel(
                    id=f"chap-{i}", project_id=pid, asset_id=aid, rank=i + 1,
                    title=f"chap {i}", hook="h", justification="j",
                    start_sec=0, end_sec=30, duration_sec=30,
                    overall_score=70, suggested_mood="neutral",
                    scene_indices_json="[0]",
                    scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
                    mezzanine_path=str(mezz),
                )
            )
        await session.commit()

    r = await api_client.post(
        f"/api/v1/projects/{pid}/montages",
        json={"reel_ids": ["chap-0", "chap-1"], "transition_duration_sec": 0.4},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "compose"
    assert body["status"] == "queued"

    # The Reel row was created (mezzanine_path points at the future output).
    r = await api_client.get(f"/api/v1/projects/{pid}/montages")
    assert r.status_code == 200
    montages = r.json()["montages"]
    assert len(montages) == 1
    assert montages[0]["child_reel_ids"] == ["chap-0", "chap-1"]
    assert montages[0]["mezzanine_ready"] is False  # job hasn't run yet


@pytest.mark.asyncio
async def test_project_reels_surface_and_clear_prompt_relevance(
    api_client, isolated_data_dir: Path
) -> None:
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "prompt-agg"})
    pid = r.json()["id"]
    aid = "p" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path=f"/tmp/{aid}.mp4",
                original_filename="p.mp4",
                duration_sec=120,
                width=1920,
                height=1080,
                fps=30,
                has_audio=True,
                size_bytes=1,
                probe_json="{}",
            )
        )
        await session.commit()

    # Prompted selection → relevance surfaces in the aggregation + DB row.
    _build_selection_on_disk(aid, pid, 2, relevance=77)
    r = await api_client.get(f"/api/v1/projects/{pid}/reels")
    assert r.status_code == 200, r.text
    payload = r.json()["reels"]
    assert all(reel["prompt_relevance"] == 77 for reel in payload)
    # v2 fields ride the same three serialization paths.
    assert all(reel["source"] == "sentence" for reel in payload)
    assert all(reel["opening_description"].startswith("literal opening") for reel in payload)
    assert all(reel["edit_style"] == "hype" for reel in payload)

    # Promptless re-select of the same candidates → values NULL-clear.
    _build_selection_on_disk(aid, pid, 2)
    r = await api_client.get(f"/api/v1/projects/{pid}/reels")
    assert all(reel["prompt_relevance"] is None for reel in r.json()["reels"])
    async with dbmod.db_state.sessionmaker() as session:
        row = await session.get(dbmod.Reel, f"reel-{aid[:4]}-0")
        assert row is not None and row.prompt_relevance is None
