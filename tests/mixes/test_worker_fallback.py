"""AI Mix CP2: compose_reel_job's reel-stub fallback + mix store."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis
import pytest

from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    REELFORGE_VERSION,
    RankedReel,
    ReelScores,
    ReelSelection,
    SelectionConfig,
)
from reelforge_core.analysis.pipeline import working_dir_for

from tests.reels._fixtures import make_analysis


def _stub_dict(mix_id: str) -> dict:
    return {
        "candidate_id": mix_id,
        "scene_indices": [],
        "start_sec": 0.0,
        "end_sec": 10.0,
        "duration_sec": 10.0,
        "title": "Mix",
        "hook": "",
        "justification": "AI mix",
        "scores": {
            "narrative_coherence": 70,
            "hook_strength": 70,
            "emotional_payoff": 70,
            "standalone_clarity": 70,
        },
        "overall": 70.0,
        "rank": 1,
        "suggested_mood": "neutral",
        "edit_style": "hype",
    }


def _seed_asset(asset_id: str, isolated_data_dir: Path) -> None:
    _seed_jobs_table(isolated_data_dir)
    analysis = make_analysis(asset_id, [30.0])
    wd = working_dir_for(asset_id)
    write_json_atomic(wd / "analysis.json", json.loads(analysis.model_dump_json()))
    # reels.json that does NOT contain the mix id.
    other = RankedReel(
        candidate_id="deadbeefdeadbeef",
        scene_indices=[0],
        start_sec=0.0,
        end_sec=30.0,
        duration_sec=30.0,
        title="t",
        hook="h",
        justification="j",
        scores=ReelScores(
            narrative_coherence=70, hook_strength=70, emotional_payoff=70, standalone_clarity=70
        ),
        overall=70.0,
        rank=1,
        suggested_mood="neutral",
    )
    sel = ReelSelection(
        asset_id=asset_id,
        analysis_source="analysis.json",
        config=SelectionConfig(),
        candidates_generated=1,
        candidates_dropped_by_dedup=0,
        reels=[other],
        anthropic_usage={},
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=0.0,
        reelforge_version=REELFORGE_VERSION,
    )
    write_json_atomic(wd / "reels.json", json.loads(sel.model_dump_json()))


def _seed_jobs_table(isolated_data_dir: Path) -> None:
    """jobstate writes to an existing jobs table (API-owned) via a cached
    thread-local connection — reset both, same as tests/test_jobstate.py."""
    import sqlite3

    from reelforge_core import jobstate

    if hasattr(jobstate._LOCAL, "conn"):
        try:
            jobstate._LOCAL.conn.close()
        except Exception:
            pass
        del jobstate._LOCAL.conn
    conn = sqlite3.connect(isolated_data_dir / "reelforge.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
             id TEXT PRIMARY KEY, arq_job_id TEXT, project_id TEXT, kind TEXT,
             asset_id TEXT, reel_id TEXT, preset_id TEXT, status TEXT,
             progress REAL, stage TEXT, message TEXT, config_json TEXT,
             result_json TEXT, error_message TEXT, error_traceback TEXT,
             logs_json TEXT, started_at TEXT, finished_at TEXT, created_at TEXT
           )"""
    )
    conn.commit()
    conn.close()


async def _run_job(monkeypatch, asset_id: str, reel_id: str, stub: dict | None):
    import apps.worker.jobs as jobs_mod

    captured: dict = {}

    async def fake_compose(asset, reel, analysis, config, progress=None):
        captured["reel"] = reel
        return SimpleNamespace(mezzanine_path="/tmp/x/mezzanine.mp4", duration_sec=10.0)

    async def fake_load_asset(aid):
        return SimpleNamespace(id=aid)

    monkeypatch.setattr(jobs_mod, "compose", fake_compose)
    monkeypatch.setattr(jobs_mod, "_load_asset", fake_load_asset)
    ctx = {"job_id": "job-test-1", "redis": fakeredis.FakeRedis(decode_responses=True)}
    result = await jobs_mod.compose_reel_job(ctx, asset_id, reel_id, {}, stub)
    return result, captured


async def test_stub_fallback_composes_synthetic_reel(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = "f" * 64
    _seed_asset(aid, isolated_data_dir)
    result, captured = await _run_job(monkeypatch, aid, "mix-abc123", _stub_dict("mix-abc123"))
    assert result["reel_id"] == "mix-abc123"
    assert isinstance(captured["reel"], RankedReel)
    assert captured["reel"].candidate_id == "mix-abc123"
    assert captured["reel"].edit_style == "hype"


async def test_missing_id_without_stub_still_raises(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = "f" * 64
    _seed_asset(aid, isolated_data_dir)
    with pytest.raises(LookupError):
        await _run_job(monkeypatch, aid, "mix-missing", None)


async def test_real_reels_json_lookup_stays_primary(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub must never shadow a real reels.json entry."""
    aid = "f" * 64
    _seed_asset(aid, isolated_data_dir)
    bogus = _stub_dict("deadbeefdeadbeef")
    bogus["title"] = "SHOULD NOT WIN"
    _, captured = await _run_job(monkeypatch, aid, "deadbeefdeadbeef", bogus)
    assert captured["reel"].title == "t"  # from reels.json, not the stub


def test_mix_store_updates_row(isolated_data_dir: Path) -> None:
    import sqlite3

    db_path = isolated_data_dir / "reelforge.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reels (
            id TEXT PRIMARY KEY, edit_json TEXT, title TEXT, hook TEXT,
            suggested_mood TEXT, edit_style TEXT, duration_sec REAL,
            end_sec REAL)"""
    )
    conn.execute(
        "INSERT INTO reels (id, title, duration_sec, end_sec) VALUES (?, ?, ?, ?)",
        ("mix-s1", "placeholder", 45.0, 45.0),
    )
    conn.commit()
    conn.close()

    from reelforge_core.mixes.store import update_mix_reel

    update_mix_reel(
        "mix-s1",
        edit_json='{"shots": []}',
        title="Two runs, one reel",
        hook="h",
        suggested_mood="energetic",
        edit_style="hype",
        duration_sec=52.5,
    )
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT * FROM reels WHERE id='mix-s1'").fetchone()
    conn.close()
    assert row[2] == "Two runs, one reel"
    assert row[5] == "hype" and row[6] == 52.5 and row[7] == 52.5
