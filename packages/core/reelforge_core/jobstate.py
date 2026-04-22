"""Shared job-state writes.

The API *creates* job rows (status=queued) before enqueuing. Workers then
transition them through running → done/failed and append log entries.

Writes go directly to the SQLite file on the /data bind mount. WAL + a 5s
busy_timeout makes the cross-process pattern safe at our load (2 workers + API).

This module lives in `reelforge_core` (not `apps/api`) so the worker
doesn't have to import API code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOGS_CAP = 200

_LOCAL = threading.local()


def _db_path() -> Path:
    return Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) / "reelforge.db"


def _conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _LOCAL.conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_running(job_id: str) -> None:
    _conn().execute(
        "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status IN ('queued','running')",
        (_now(), job_id),
    )


def _record_success(job_id: str, result: dict[str, Any]) -> None:
    _conn().execute(
        """UPDATE jobs
              SET status='done',
                  finished_at=?,
                  result_json=?,
                  error_message=NULL,
                  error_traceback=NULL,
                  progress=1.0,
                  stage='done',
                  message='done'
            WHERE id=?""",
        (_now(), json.dumps(result), job_id),
    )


def _record_failure(job_id: str, message: str, traceback_: str) -> None:
    _conn().execute(
        """UPDATE jobs
              SET status='failed',
                  finished_at=?,
                  error_message=?,
                  error_traceback=?,
                  stage='error',
                  message=?
            WHERE id=?""",
        (_now(), message, traceback_, message[:500], job_id),
    )


def _append_log(job_id: str, level: str, msg: str) -> None:
    row = _conn().execute(
        "SELECT logs_json FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        return
    try:
        logs = json.loads(row["logs_json"] or "[]")
    except json.JSONDecodeError:
        logs = []
    logs.append({"ts": _now(), "level": level, "msg": msg})
    if len(logs) > LOGS_CAP:
        logs = logs[-LOGS_CAP:]
    _conn().execute(
        "UPDATE jobs SET logs_json=? WHERE id=?",
        (json.dumps(logs, ensure_ascii=False), job_id),
    )


# ---------------------------------------------------------------------------
# Async wrappers — all blocking SQLite calls go through to_thread.
# ---------------------------------------------------------------------------


async def mark_job_running(job_id: str) -> None:
    await asyncio.to_thread(_mark_running, job_id)


async def record_job_success(job_id: str, result: dict[str, Any]) -> None:
    await asyncio.to_thread(_record_success, job_id, result)


async def record_job_failure(job_id: str, message: str, traceback_: str) -> None:
    await asyncio.to_thread(_record_failure, job_id, message, traceback_)


async def append_log(job_id: str, level: str, msg: str) -> None:
    await asyncio.to_thread(_append_log, job_id, level, msg)
