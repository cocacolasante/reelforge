"""Durable store for real-world performance labels (Phase 7 ingest).

No training changes: this exists so ground truth accumulates now and is waiting
when the listwise ranker is ready to learn from it.

Keyed by `reelforge_clip_id`, deliberately. The existing offline eval labels in
`tests/reels/eval/labels/` key on raw start/end spans, which drift the moment
boundary refinement moves an edge; `candidate_id` does not move.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from apps.queue_consumer.contract import LabelIngest

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_performance_labels (
    reelforge_clip_id TEXT NOT NULL,
    observed_at       TEXT NOT NULL,
    asset_id          TEXT,
    tenant_id         TEXT,
    completion_rate   REAL,
    shares_per_view   REAL,
    watch_time_pct    REAL,
    verdict           TEXT,
    raw               TEXT NOT NULL,
    ingested_at       TEXT NOT NULL DEFAULT (datetime('now')),
    -- Append-only per observation, but re-sending the same observation is a
    -- no-op: growth-agent may re-export a label after a retry.
    PRIMARY KEY (reelforge_clip_id, observed_at)
);
CREATE INDEX IF NOT EXISTS clip_labels_verdict_idx
    ON clip_performance_labels (verdict);
"""


def db_path() -> Path:
    data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    return Path(os.environ.get("REELFORGE_LABELS_DB", data_dir / "labels.sqlite3"))


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def store(label: LabelIngest) -> None:
    observed = label.observed_at or "unknown"
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO clip_performance_labels
                (reelforge_clip_id, observed_at, asset_id, tenant_id,
                 completion_rate, shares_per_view, watch_time_pct, verdict, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (reelforge_clip_id, observed_at) DO UPDATE SET
                completion_rate = excluded.completion_rate,
                shares_per_view = excluded.shares_per_view,
                watch_time_pct  = excluded.watch_time_pct,
                verdict         = excluded.verdict,
                raw             = excluded.raw
            """,
            (
                label.reelforge_clip_id,
                observed,
                label.asset_id,
                label.tenant_id,
                label.labels.completion_rate,
                label.labels.shares_per_view,
                label.labels.watch_time_pct,
                label.labels.verdict,
                json.dumps(label.model_dump(by_alias=True)),
            ),
        )
    log.info(
        "stored label for clip %s (verdict=%s)", label.reelforge_clip_id, label.labels.verdict
    )


def count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT count(*) FROM clip_performance_labels").fetchone()[0])
