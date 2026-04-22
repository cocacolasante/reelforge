"""Content-addressed file cache with LRU eviction.

Used by compose for:
- Normalized clip files (key = asset_id + scene_index + source_mtime + aspect + fps + resolution)
- Trimmed/faded music (key = track_id + target_duration + volume + fades)

Entries live on the /data volume; a sidecar SQLite table tracks last_accessed_at
+ size so eviction is O(log n) on access.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCAL = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cache (
    cache_key         TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,  -- 'clip' | 'music' | ...
    path              TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    last_accessed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_cache_kind_accessed
    ON file_cache(kind, last_accessed_at);
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


def _cache_root() -> Path:
    p = Path(os.environ.get("REELFORGE_DATA_DIR", "/data")) / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def compute_key(kind: str, parts: dict) -> str:
    """Hash the kind + sorted key/value parts into a hex digest."""
    canon = "|".join(f"{k}={parts[k]}" for k in sorted(parts))
    return hashlib.sha256(f"{kind}|{canon}".encode("utf-8")).hexdigest()[:32]


def path_for(kind: str, key: str, ext: str) -> Path:
    kind_dir = _cache_root() / kind
    kind_dir.mkdir(parents=True, exist_ok=True)
    return kind_dir / f"{key}.{ext.lstrip('.')}"


def lookup(key: str) -> Path | None:
    """Return the cached file path if present and still exists on disk."""
    row = _conn().execute(
        "SELECT path FROM file_cache WHERE cache_key=?", (key,)
    ).fetchone()
    if row is None:
        return None
    p = Path(row["path"])
    if not p.exists():
        _conn().execute("DELETE FROM file_cache WHERE cache_key=?", (key,))
        return None
    touch(key)
    return p


def touch(key: str) -> None:
    _conn().execute(
        "UPDATE file_cache SET last_accessed_at=? WHERE cache_key=?",
        (datetime.now(timezone.utc).isoformat(), key),
    )


def register(key: str, kind: str, path: Path) -> None:
    size = path.stat().st_size if path.exists() else 0
    now = datetime.now(timezone.utc).isoformat()
    _conn().execute(
        """
        INSERT INTO file_cache (cache_key, kind, path, size_bytes, created_at, last_accessed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
          path=excluded.path,
          size_bytes=excluded.size_bytes,
          last_accessed_at=excluded.last_accessed_at
        """,
        (key, kind, str(path), size, now, now),
    )


def evict_if_over_cap(kind: str, cap_bytes: int) -> int:
    """Evict oldest entries of `kind` until total size is at or below cap_bytes.
    Returns bytes freed."""
    rows = _conn().execute(
        "SELECT cache_key, path, size_bytes FROM file_cache WHERE kind=? "
        "ORDER BY last_accessed_at ASC",
        (kind,),
    ).fetchall()
    total = sum(int(r["size_bytes"] or 0) for r in rows)
    freed = 0
    for r in rows:
        if total <= cap_bytes:
            break
        try:
            Path(r["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        _conn().execute("DELETE FROM file_cache WHERE cache_key=?", (r["cache_key"],))
        total -= int(r["size_bytes"] or 0)
        freed += int(r["size_bytes"] or 0)
    return freed


def cache_size(kind: str | None = None) -> int:
    q = "SELECT COALESCE(SUM(size_bytes), 0) AS s FROM file_cache"
    params: tuple = ()
    if kind is not None:
        q += " WHERE kind=?"
        params = (kind,)
    row = _conn().execute(q, params).fetchone()
    return int(row["s"] or 0)


def cap_from_env(kind: str, default_gb: float) -> int:
    """Return the cap in bytes for a cache kind, reading env overrides."""
    var = {
        "clip": "CACHE_CLIPS_GB",
        "music": "CACHE_MUSIC_GB",
        "caption_preview": "CACHE_PREVIEWS_GB",
    }.get(kind)
    val = os.environ.get(var, "") if var else ""
    try:
        gb = float(val) if val else default_gb
    except ValueError:
        gb = default_gb
    return int(gb * (1024**3))


def purge_kind(kind: str) -> int:
    """Remove every entry of `kind`. Returns bytes freed."""
    rows = _conn().execute(
        "SELECT cache_key, path, size_bytes FROM file_cache WHERE kind=?", (kind,)
    ).fetchall()
    freed = 0
    for r in rows:
        try:
            Path(r["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        _conn().execute("DELETE FROM file_cache WHERE cache_key=?", (r["cache_key"],))
        freed += int(r["size_bytes"] or 0)
    # Also nuke the on-disk directory if it's now empty
    d = _cache_root() / kind
    try:
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass
    return freed
