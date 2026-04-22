from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reelforge_core import usage


@pytest.fixture
def usage_db(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    # Fresh sqlite file per test; also zero out the module-level thread-local
    # connection so a new one opens at the new path.
    for mod in (usage,):
        if hasattr(mod._LOCAL, "conn"):
            try:
                mod._LOCAL.conn.close()
            except Exception:
                pass
            del mod._LOCAL.conn
    # Ensure the usage.py reads the new REELFORGE_DATA_DIR.
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(isolated_data_dir))
    return isolated_data_dir


async def test_record_and_aggregate_single_job(usage_db: Path) -> None:
    await usage.record_anthropic_usage(
        job_id="job1",
        model="claude-haiku-4-5",
        input_tokens=1000,
        output_tokens=500,
        cache_hits=0,
        project_id="proj1",
        asset_id="a1",
    )
    agg = await usage.aggregate_usage(project_id="proj1")
    assert agg["total_input_tokens"] == 1000
    assert agg["total_output_tokens"] == 500
    assert agg["total_calls"] == 1
    assert agg["estimated_total_cost_usd"] > 0


async def test_aggregate_groups_by_model(usage_db: Path) -> None:
    await usage.record_anthropic_usage(
        job_id="j1",
        model="claude-haiku-4-5",
        input_tokens=10,
        output_tokens=10,
        project_id="p1",
    )
    await usage.record_anthropic_usage(
        job_id="j2",
        model="claude-sonnet-4-5",
        input_tokens=20,
        output_tokens=20,
        project_id="p1",
    )
    agg = await usage.aggregate_usage(project_id="p1")
    models = {m["model"] for m in agg["by_model"]}
    assert models == {"claude-haiku-4-5", "claude-sonnet-4-5"}
    assert agg["total_calls"] == 2


async def test_aggregate_filters_by_project(usage_db: Path) -> None:
    await usage.record_anthropic_usage(
        job_id="j1", model="claude-haiku-4-5", input_tokens=5, output_tokens=5, project_id="A"
    )
    await usage.record_anthropic_usage(
        job_id="j2", model="claude-haiku-4-5", input_tokens=99, output_tokens=99, project_id="B"
    )
    a = await usage.aggregate_usage(project_id="A")
    b = await usage.aggregate_usage(project_id="B")
    assert a["total_input_tokens"] == 5
    assert b["total_input_tokens"] == 99


async def test_aggregate_empty_returns_zeros(usage_db: Path) -> None:
    agg = await usage.aggregate_usage(project_id="none")
    assert agg["total_input_tokens"] == 0
    assert agg["total_output_tokens"] == 0
    assert agg["total_calls"] == 0
    assert agg["estimated_total_cost_usd"] == 0.0


async def test_usage_backfills_from_jobs_row(usage_db: Path) -> None:
    # Pre-seed a jobs row so record_anthropic_usage can infer project/asset
    # when the caller doesn't supply them.
    conn = sqlite3.connect(usage_db / "reelforge.db")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            arq_job_id TEXT,
            project_id TEXT,
            kind TEXT,
            asset_id TEXT,
            reel_id TEXT,
            preset_id TEXT,
            status TEXT,
            progress REAL,
            stage TEXT,
            message TEXT,
            config_json TEXT,
            result_json TEXT,
            error_message TEXT,
            error_traceback TEXT,
            logs_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO jobs (id, kind, status, project_id, asset_id, progress, logs_json, created_at) "
        "VALUES ('JOB7', 'analyze', 'done', 'pX', 'aY', 1.0, '[]', '')"
    )
    conn.commit()
    conn.close()

    await usage.record_anthropic_usage(
        job_id="JOB7",
        model="claude-haiku-4-5",
        input_tokens=42,
        output_tokens=17,
    )
    agg = await usage.aggregate_usage(project_id="pX")
    assert agg["total_input_tokens"] == 42
    assert agg["total_output_tokens"] == 17


async def test_aggregate_by_job_id(usage_db: Path) -> None:
    await usage.record_anthropic_usage(
        job_id="jobA", model="claude-haiku-4-5", input_tokens=11, output_tokens=22, project_id="p"
    )
    await usage.record_anthropic_usage(
        job_id="jobB", model="claude-haiku-4-5", input_tokens=1, output_tokens=2, project_id="p"
    )
    only_a = await usage.aggregate_usage(job_id="jobA")
    assert only_a["total_input_tokens"] == 11
    assert only_a["total_output_tokens"] == 22
