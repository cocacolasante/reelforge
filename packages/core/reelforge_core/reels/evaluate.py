"""Selection-quality evaluation: recall@K against hand-labeled ground truth.

Labels live one file per asset (``tests/reels/eval/labels/<asset_id>.json``):

    {
      "asset_id": "e0bc0924...",
      "picks": [
        {"start_sec": 12.0, "end_sec": 47.5, "note": "the big crash"}
      ]
    }

A labeled pick counts as *recalled* at K when at least one of the top-K reels
in that asset's ``reels.json`` overlaps it by >= ``MIN_OVERLAP_FRACTION`` of
the pick's own duration (intersection / pick duration).

Pure math lives in :func:`overlap_fraction` / :func:`recall_at_k`; the file
loaders are thin and kept here so the CLI and ``scripts/eval_selection.py``
share one implementation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from reelforge_core.models import RankedReel, ReelSelection

log = logging.getLogger(__name__)

RECALL_KS: tuple[int, ...] = (3, 5, 10)
MIN_OVERLAP_FRACTION = 0.5


@dataclass
class LabelPick:
    start_sec: float
    end_sec: float
    note: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass
class AssetLabels:
    asset_id: str
    picks: list[LabelPick]


@dataclass
class AssetEval:
    asset_id: str
    n_picks: int
    recall_at: dict[int, float] = field(default_factory=dict)
    candidates_generated: int | None = None
    elapsed_sec: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    n_reels: int = 0
    resumed_zero_tokens: bool = False
    error: str | None = None


def overlap_fraction(
    pick_start: float, pick_end: float, reel_start: float, reel_end: float
) -> float:
    """Intersection of the two spans as a fraction of the PICK's duration."""
    pick_dur = pick_end - pick_start
    if pick_dur <= 0:
        return 0.0
    inter = min(pick_end, reel_end) - max(pick_start, reel_start)
    return max(0.0, inter) / pick_dur


def pick_recalled(pick: LabelPick, reels: list[RankedReel]) -> bool:
    return any(
        overlap_fraction(pick.start_sec, pick.end_sec, r.start_sec, r.end_sec)
        >= MIN_OVERLAP_FRACTION
        for r in reels
    )


def recall_at_k(picks: list[LabelPick], reels: list[RankedReel], k: int) -> float:
    """Fraction of picks recalled by the top-k reels (by rank)."""
    if not picks:
        return 0.0
    top = sorted(reels, key=lambda r: r.rank)[:k]
    return sum(1 for p in picks if pick_recalled(p, top)) / len(picks)


def load_labels(labels_dir: Path) -> list[AssetLabels]:
    """Load every ``*.json`` label file in the directory (sorted by name)."""
    out: list[AssetLabels] = []
    for path in sorted(labels_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            picks = [
                LabelPick(
                    start_sec=float(p["start_sec"]),
                    end_sec=float(p["end_sec"]),
                    note=str(p.get("note", "")),
                )
                for p in data["picks"]
            ]
            out.append(AssetLabels(asset_id=str(data["asset_id"]), picks=picks))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("skipping malformed label file %s: %s", path, exc)
    return out


def evaluate_asset(labels: AssetLabels, data_dir: Path) -> AssetEval:
    ev = AssetEval(asset_id=labels.asset_id, n_picks=len(labels.picks))
    reels_path = data_dir / "working" / labels.asset_id / "reels.json"
    if not reels_path.exists():
        ev.error = "no reels.json"
        return ev
    try:
        selection = ReelSelection.model_validate_json(reels_path.read_text())
    except Exception as exc:  # malformed reels.json should not kill the run
        ev.error = f"unparseable reels.json: {exc}"
        return ev
    ev.candidates_generated = selection.candidates_generated
    ev.elapsed_sec = selection.elapsed_sec
    usage = selection.anthropic_usage or {}
    ev.input_tokens = int(usage.get("input_tokens", 0))
    ev.output_tokens = int(usage.get("output_tokens", 0))
    ev.n_reels = len(selection.reels)
    # A --resume run re-coerces ranking_raw.json and reports zero tokens;
    # flag it so "0 tokens" never reads as "this selection was free".
    ev.resumed_zero_tokens = (
        ev.input_tokens == 0 and ev.output_tokens == 0 and bool(selection.reels)
    )
    for k in RECALL_KS:
        ev.recall_at[k] = recall_at_k(labels.picks, selection.reels, k)
    return ev


def evaluate_all(labels_dir: Path, data_dir: Path) -> list[AssetEval]:
    return [evaluate_asset(al, data_dir) for al in load_labels(labels_dir)]


def format_report(evals: list[AssetEval]) -> str:
    """Plain-text table: per-asset recall@K + cost columns + overall means."""
    if not evals:
        return "no label files found"
    header = (
        f"{'asset':<18} {'picks':>5} "
        + " ".join(f"{'R@' + str(k):>5}" for k in RECALL_KS)
        + f" {'cands':>6} {'reels':>5} {'elapsed':>8} {'tokens in/out':>15}"
    )
    lines = [header, "-" * len(header)]
    scored = [e for e in evals if e.error is None]
    for e in evals:
        if e.error is not None:
            lines.append(f"{e.asset_id[:16] + '…':<18} {e.n_picks:>5} ERROR: {e.error}")
            continue
        tok = f"{e.input_tokens}/{e.output_tokens}"
        if e.resumed_zero_tokens:
            tok += "*"
        lines.append(
            f"{e.asset_id[:16] + '…':<18} {e.n_picks:>5} "
            + " ".join(f"{e.recall_at[k]:>5.2f}" for k in RECALL_KS)
            + f" {e.candidates_generated if e.candidates_generated is not None else '-':>6}"
            + f" {e.n_reels:>5}"
            + f" {(f'{e.elapsed_sec:.1f}s' if e.elapsed_sec is not None else '-'):>8}"
            + f" {tok:>15}"
        )
    if scored:
        lines.append("-" * len(header))
        lines.append(
            f"{'mean':<18} {sum(e.n_picks for e in scored):>5} "
            + " ".join(
                f"{sum(e.recall_at[k] for e in scored) / len(scored):>5.2f}"
                for k in RECALL_KS
            )
        )
    if any(e.resumed_zero_tokens for e in evals):
        lines.append("* zero tokens: reels.json came from a --resume run; the")
        lines.append("  original ranking call's cost is not re-counted.")
    return "\n".join(lines)
