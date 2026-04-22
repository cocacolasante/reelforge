"""User-edited transcript overrides. Shared between the API (write + read via
aiosqlite) and the worker (read-only via sqlite3 at compose time)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from reelforge_core.models import Transcript

_LOCAL = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_overrides (
    asset_id         TEXT PRIMARY KEY,
    transcript_json  TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""


def _db_path() -> Path:
    return Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) / "reelforge.db"


def _conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        p = _db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        _LOCAL.conn = conn
    return conn


def _load_sync(asset_id: str) -> Transcript | None:
    row = _conn().execute(
        "SELECT transcript_json FROM transcript_overrides WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return Transcript.model_validate_json(row["transcript_json"])
    except Exception:
        return None


def _save_sync(asset_id: str, transcript: Transcript) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _conn().execute(
        """INSERT INTO transcript_overrides (asset_id, transcript_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(asset_id) DO UPDATE SET
             transcript_json=excluded.transcript_json,
             updated_at=excluded.updated_at""",
        (asset_id, transcript.model_dump_json(), now),
    )


def _delete_sync(asset_id: str) -> int:
    cur = _conn().execute(
        "DELETE FROM transcript_overrides WHERE asset_id=?", (asset_id,)
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Sync API for the compose pipeline (worker side)
# ---------------------------------------------------------------------------


def load_override_sync(asset_id: str) -> Transcript | None:
    return _load_sync(asset_id)


# ---------------------------------------------------------------------------
# Async API for the FastAPI side
# ---------------------------------------------------------------------------


async def load_override(asset_id: str) -> Transcript | None:
    return await asyncio.to_thread(_load_sync, asset_id)


async def save_override(asset_id: str, transcript: Transcript) -> None:
    await asyncio.to_thread(_save_sync, asset_id, transcript)


async def delete_override(asset_id: str) -> int:
    return await asyncio.to_thread(_delete_sync, asset_id)


def validate_transcript(t: Transcript) -> None:
    """Raise ValueError if the transcript violates the PUT contract."""
    last_end = -1.0
    for seg in t.segments:
        if seg.end < seg.start:
            raise ValueError(f"segment end {seg.end} before start {seg.start}")
        if seg.start < last_end - 1e-3:
            raise ValueError(f"non-monotonic segment at {seg.start} after {last_end}")
        for w in seg.words:
            if w.end < w.start:
                raise ValueError(f"word end {w.end} before start {w.start}")
            if w.start < 0:
                raise ValueError(f"word start {w.start} is negative")
        last_end = seg.end
