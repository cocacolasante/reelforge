"""Labelled rows: each clip's latest performance label joined with its
ranking scores (reels.json) and its render's QA metrics (qa.json)."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

TARGETS = ("completion_rate", "shares_per_view", "watch_time_pct")
SCORE_FIELDS = ("hook_strength", "narrative_coherence", "emotional_payoff", "standalone_clarity")


@dataclass
class Row:
    clip_id: str
    targets: dict[str, float | None]
    features: dict[str, float] = field(default_factory=dict)
    reel: dict = field(default_factory=dict)  # the reels.json entry
    verdict: str | None = None


def labels_db(data_dir: Path) -> Path:
    return Path(os.environ.get("REELFORGE_LABELS_DB", data_dir / "labels.sqlite3"))


def latest_labels(db: Path) -> dict[str, dict]:
    """clip id -> its most recent observation. {} when there's no database."""
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT reelforge_clip_id, observed_at, completion_rate, shares_per_view,
                      watch_time_pct, verdict
               FROM clip_performance_labels ORDER BY observed_at"""
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for cid, observed, comp, shares, watch, verdict in rows:
        out[cid] = {"observed_at": observed, "completion_rate": comp,
                    "shares_per_view": shares, "watch_time_pct": watch, "verdict": verdict}
    return out


def reel_index(working: Path) -> dict[str, tuple[str, dict]]:
    """candidate_id -> (asset_id, reels.json entry) across every asset."""
    out: dict[str, tuple[str, dict]] = {}
    for path in working.glob("*/reels.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for r in data.get("reels", []):
            if r.get("candidate_id"):
                out[r["candidate_id"]] = (path.parent.name, r)
    return out


def features_for(reel: dict, qa: dict | None) -> dict[str, float]:
    """Numeric signals: the ranker's scores and extras, and the QA scorecard's
    measurements. Missing values are simply absent. Pure."""
    f: dict[str, float] = {}
    scores = reel.get("scores") or {}
    for k in SCORE_FIELDS:
        if isinstance(scores.get(k), (int, float)):
            f[k] = float(scores[k])
    for k in ("overall", "duration_sec", "rank_position", "ending_lands", "prompt_relevance"):
        if isinstance(reel.get(k), (int, float)):
            f[k] = float(reel[k])
    f["has_cold_open"] = 1.0 if reel.get("cold_open") else 0.0
    for check in (qa or {}).get("checks", []):
        if isinstance(check.get("value"), (int, float)):
            f[f"qa_{check['metric']}"] = float(check["value"])
    return f


def collect(data_dir: Path) -> list[Row]:
    """Every labelled clip we can join to its reel. Pure given the files."""
    labels = latest_labels(labels_db(data_dir))
    if not labels:
        return []
    working = data_dir / "working"
    index = reel_index(working)
    rows: list[Row] = []
    for cid, lab in labels.items():
        asset_id, reel = index.get(cid, (None, {}))
        qa = None
        if asset_id:
            qa_path = working / asset_id / "reels" / cid / "qa.json"
            try:
                qa = json.loads(qa_path.read_text())
            except (OSError, ValueError):
                qa = None
        rows.append(Row(
            clip_id=cid,
            targets={t: lab.get(t) for t in TARGETS},
            features=features_for(reel, qa) if reel else {},
            reel=reel,
            verdict=lab.get("verdict"),
        ))
    return rows
