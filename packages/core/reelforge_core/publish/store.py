"""Sync SQLite access to social_accounts + publications for the worker.

Same pattern as jobstate.py: direct writes to the shared /data DB with WAL +
busy_timeout. The API owns table creation (SQLModel create_all); these
helpers only read/update rows the API already created.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCAL = threading.local()


def _db_path() -> Path:
    return Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) / "reelforge.db"


def _conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_db_path(), check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _LOCAL.conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_account(platform: str) -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT * FROM social_accounts WHERE platform = ?", (platform,)
    ).fetchone()
    return dict(row) if row else None


def update_account_access_token(platform: str, access_token: str) -> None:
    _conn().execute(
        "UPDATE social_accounts SET access_token = ? WHERE platform = ?",
        (access_token, platform),
    )


def get_publication(publication_id: str) -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT * FROM publications WHERE id = ?", (publication_id,)
    ).fetchone()
    return dict(row) if row else None


def mark_publication_running(publication_id: str) -> None:
    _conn().execute(
        "UPDATE publications SET status = 'uploading' WHERE id = ?",
        (publication_id,),
    )


def mark_publication_done(publication_id: str, video_id: str, video_url: str) -> None:
    _conn().execute(
        "UPDATE publications SET status = 'done', video_id = ?, video_url = ?, "
        "completed_at = ?, error_message = NULL WHERE id = ?",
        (video_id, video_url, _now(), publication_id),
    )


def mark_publication_failed(publication_id: str, error: str) -> None:
    _conn().execute(
        "UPDATE publications SET status = 'failed', error_message = ?, "
        "completed_at = ? WHERE id = ?",
        (error[:2000], _now(), publication_id),
    )
