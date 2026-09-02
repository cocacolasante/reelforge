"""Sync SQLite access to the `reels` row for the mix worker.

Same pattern as publish/store.py / jobstate.py: direct writes to the shared
/data DB with WAL + busy_timeout. The API creates the mix Reel row (with a
placeholder title and no edit_json); the worker fills in the sequenced
result here before rendering.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

_LOCAL = threading.local()


def _db_path() -> Path:
    return Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) / "reelforge.db"


def _conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            _db_path(), check_same_thread=False, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _LOCAL.conn = conn
    return conn


def update_mix_reel(
    reel_id: str,
    *,
    edit_json: str,
    title: str,
    hook: str,
    suggested_mood: str,
    edit_style: str,
    duration_sec: float,
) -> None:
    """Persist the sequenced mix onto its Reel row. end_sec mirrors the mix's
    edited duration so trim/duration displays stay sane."""
    _conn().execute(
        """
        UPDATE reels
        SET edit_json = ?, title = ?, hook = ?, suggested_mood = ?,
            edit_style = ?, duration_sec = ?, end_sec = ?
        WHERE id = ?
        """,
        (edit_json, title, hook, suggested_mood, edit_style, duration_sec, duration_sec, reel_id),
    )
