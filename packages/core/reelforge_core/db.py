"""SQLite-backed semantics cache.

Phase 1 originally owned the `jobs` table here; Phase 5 unified the jobs schema
on the API side (see `apps/api/db.py`). Job-state writes go through
`reelforge_core.jobstate` instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# Re-export the Phase 5 job-state helpers so existing `from reelforge_core import db`
# imports that reach for `db.record_job_*` still work.
from reelforge_core.jobstate import (  # noqa: F401
    append_log,
    mark_job_running,
    record_job_failure,
    record_job_success,
)

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) / "reelforge.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantics_cache (
    cache_key       TEXT PRIMARY KEY,
    result_json     TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantics_model
    ON semantics_cache(model, prompt_version);
"""


_LOCAL = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        conn.executescript(_SCHEMA)
        _LOCAL.conn = conn
    return conn


def init_db() -> None:
    """Ensure `semantics_cache` exists. Safe to call from any worker boot."""
    get_conn()


# ---------------------------------------------------------------------------
# Semantics cache
# ---------------------------------------------------------------------------


def semantics_cache_key(
    *,
    asset_id: str,
    scene_index: int,
    model: str,
    prompt_version: str,
    thumb_sha256: str,
    transcript_slice_sha256: str,
) -> str:
    raw = (
        f"{asset_id}|{scene_index}|{model}|{prompt_version}"
        f"|{thumb_sha256}|{transcript_slice_sha256}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fetch_semantics_sync(keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT cache_key, result_json FROM semantics_cache WHERE cache_key IN ({placeholders})",
        keys,
    ).fetchall()
    return {row["cache_key"]: json.loads(row["result_json"]) for row in rows}


async def fetch_semantics(keys: list[str]) -> dict[str, dict]:
    return await asyncio.to_thread(_fetch_semantics_sync, keys)


def _upsert_semantics_sync(
    *, cache_key: str, result: dict, model: str, prompt_version: str
) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO semantics_cache (cache_key, result_json, model, prompt_version, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            result_json=excluded.result_json,
            model=excluded.model,
            prompt_version=excluded.prompt_version,
            created_at=excluded.created_at
        """,
        (
            cache_key,
            json.dumps(result, ensure_ascii=False),
            model,
            prompt_version,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


async def upsert_semantics(
    *, cache_key: str, result: dict, model: str, prompt_version: str
) -> None:
    await asyncio.to_thread(
        _upsert_semantics_sync,
        cache_key=cache_key,
        result=result,
        model=model,
        prompt_version=prompt_version,
    )


# ---------------------------------------------------------------------------
# Backwards-compat shims
# ---------------------------------------------------------------------------


async def record_job_start(job_id: str, kind: str, asset_id: str | None) -> None:
    """Preserved for Phase 1-era worker code; now just marks running (the API
    creates the row at enqueue time)."""
    await mark_job_running(job_id)
