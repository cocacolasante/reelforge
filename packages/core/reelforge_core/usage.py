"""Anthropic usage bookkeeping. Rows are written from the worker after each
successful API call; the API aggregates them for UI display."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCAL = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anthropic_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    asset_id        TEXT,
    project_id      TEXT,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cache_hits      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_project ON anthropic_usage(project_id);
CREATE INDEX IF NOT EXISTS idx_usage_job ON anthropic_usage(job_id);
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


def _record_sync(
    *,
    job_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hits: int,
    asset_id: str | None,
    project_id: str | None,
) -> None:
    conn = _conn()
    # Fill in project_id/asset_id from the jobs row if the caller didn't supply them.
    # The jobs table is owned by the API layer; tolerate its absence so worker
    # boots (or tests) that run before the API has ever touched the DB still work.
    if project_id is None or asset_id is None:
        try:
            row = conn.execute(
                "SELECT project_id, asset_id FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            if project_id is None:
                project_id = row["project_id"]
            if asset_id is None:
                asset_id = row["asset_id"]
    conn.execute(
        """
        INSERT INTO anthropic_usage
          (job_id, asset_id, project_id, model, input_tokens, output_tokens, cache_hits, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            asset_id,
            project_id,
            model,
            int(input_tokens or 0),
            int(output_tokens or 0),
            int(cache_hits or 0),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


async def record_anthropic_usage(
    *,
    job_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hits: int = 0,
    asset_id: str | None = None,
    project_id: str | None = None,
) -> None:
    await asyncio.to_thread(
        _record_sync,
        job_id=job_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hits=cache_hits,
        asset_id=asset_id,
        project_id=project_id,
    )


def _aggregate_sync(project_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
    clauses = []
    params: list[Any] = []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if job_id:
        clauses.append("job_id = ?")
        params.append(job_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _conn().execute(
        f"""
        SELECT model,
               SUM(input_tokens)  AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_hits)    AS cache_hits,
               COUNT(*)           AS call_count
        FROM anthropic_usage
        {where}
        GROUP BY model
        """,
        params,
    ).fetchall()
    from reelforge_core.pricing import price_for

    by_model = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    total_calls = 0
    total_cache_hits = 0
    for r in rows:
        inp = int(r["input_tokens"] or 0)
        out = int(r["output_tokens"] or 0)
        cost = price_for(r["model"], inp, out)
        by_model.append(
            {
                "model": r["model"],
                "input_tokens": inp,
                "output_tokens": out,
                "cache_hits": int(r["cache_hits"] or 0),
                "call_count": int(r["call_count"] or 0),
                "estimated_cost_usd": cost,
            }
        )
        total_in += inp
        total_out += out
        total_cost += cost
        total_calls += int(r["call_count"] or 0)
        total_cache_hits += int(r["cache_hits"] or 0)
    return {
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_hits": total_cache_hits,
        "total_calls": total_calls,
        "estimated_total_cost_usd": round(total_cost, 4),
        "by_model": by_model,
    }


async def aggregate_usage(
    *, project_id: str | None = None, job_id: str | None = None
) -> dict[str, Any]:
    return await asyncio.to_thread(_aggregate_sync, project_id=project_id, job_id=job_id)
