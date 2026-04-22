from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from reelforge_core import jobstate


@pytest.fixture
def jobstate_env(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    if hasattr(jobstate._LOCAL, "conn"):
        try:
            jobstate._LOCAL.conn.close()
        except Exception:
            pass
        del jobstate._LOCAL.conn
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(isolated_data_dir))
    # Seed a jobs table + row. jobstate writes to an existing table.
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
    conn.execute(
        "INSERT INTO jobs (id, status, logs_json, created_at) VALUES (?, 'queued', '[]', '')",
        ("JOB-A",),
    )
    conn.commit()
    conn.close()
    return isolated_data_dir


async def test_mark_running(jobstate_env: Path) -> None:
    await jobstate.mark_job_running("JOB-A")
    conn = sqlite3.connect(jobstate_env / "reelforge.db")
    row = conn.execute("SELECT status, started_at FROM jobs WHERE id=?", ("JOB-A",)).fetchone()
    assert row[0] == "running"
    assert row[1] is not None


async def test_mark_running_does_not_downgrade_terminal(jobstate_env: Path) -> None:
    # After done, mark_running should NOT flip it back.
    await jobstate.mark_job_running("JOB-A")
    await jobstate.record_job_success("JOB-A", {"ok": True})
    await jobstate.mark_job_running("JOB-A")
    conn = sqlite3.connect(jobstate_env / "reelforge.db")
    row = conn.execute("SELECT status FROM jobs WHERE id=?", ("JOB-A",)).fetchone()
    assert row[0] == "done"


async def test_record_success_writes_result(jobstate_env: Path) -> None:
    await jobstate.record_job_success("JOB-A", {"foo": 42})
    conn = sqlite3.connect(jobstate_env / "reelforge.db")
    row = conn.execute(
        "SELECT status, result_json, progress, stage, message FROM jobs WHERE id=?",
        ("JOB-A",),
    ).fetchone()
    assert row[0] == "done"
    assert json.loads(row[1])["foo"] == 42
    assert row[2] == 1.0
    assert row[3] == "done"


async def test_record_failure_writes_traceback(jobstate_env: Path) -> None:
    await jobstate.record_job_failure("JOB-A", "boom", "traceback-here")
    conn = sqlite3.connect(jobstate_env / "reelforge.db")
    row = conn.execute(
        "SELECT status, error_message, error_traceback, message, stage FROM jobs WHERE id=?",
        ("JOB-A",),
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == "boom"
    assert row[2] == "traceback-here"
    assert row[3] == "boom"
    assert row[4] == "error"


async def test_append_log_caps_at_200(jobstate_env: Path) -> None:
    for i in range(250):
        await jobstate.append_log("JOB-A", "INFO", f"line-{i}")
    conn = sqlite3.connect(jobstate_env / "reelforge.db")
    row = conn.execute("SELECT logs_json FROM jobs WHERE id=?", ("JOB-A",)).fetchone()
    logs = json.loads(row[0])
    assert len(logs) == jobstate.LOGS_CAP
    # Latest entries are at the tail
    assert logs[-1]["msg"] == "line-249"


async def test_append_log_missing_job_is_noop(jobstate_env: Path) -> None:
    # Should not raise; just silently skip.
    await jobstate.append_log("DOES-NOT-EXIST", "INFO", "stray")
    conn = sqlite3.connect(jobstate_env / "reelforge.db")
    row = conn.execute(
        "SELECT logs_json FROM jobs WHERE id=?", ("JOB-A",)
    ).fetchone()
    assert row[0] == "[]"  # unchanged
