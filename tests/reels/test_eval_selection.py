"""Unit tests for the selection eval harness (reels/evaluate.py)."""

from __future__ import annotations

import json
from pathlib import Path

from reelforge_core.models import RankedReel, ReelScores, ReelSelection, SelectionConfig
from reelforge_core.reels.evaluate import (
    AssetLabels,
    LabelPick,
    evaluate_asset,
    format_report,
    load_labels,
    overlap_fraction,
    recall_at_k,
)


def _reel(cid: str, start: float, end: float, rank: int) -> RankedReel:
    return RankedReel(
        candidate_id=cid,
        scene_indices=[0],
        start_sec=start,
        end_sec=end,
        duration_sec=end - start,
        title="t",
        hook="h",
        justification="j",
        scores=ReelScores(
            narrative_coherence=50, hook_strength=50, emotional_payoff=50, standalone_clarity=50
        ),
        overall=50.0,
        rank=rank,
        suggested_mood="neutral",
    )


def _selection(asset_id: str, reels: list[RankedReel], tokens=(100, 50)) -> ReelSelection:
    return ReelSelection(
        asset_id=asset_id,
        analysis_source="analysis.json",
        config=SelectionConfig(),
        candidates_generated=len(reels),
        candidates_dropped_by_dedup=0,
        reels=reels,
        anthropic_usage={"input_tokens": tokens[0], "output_tokens": tokens[1], "cache_hits": 0},
        created_at="2026-08-27T00:00:00Z",
        elapsed_sec=1.5,
        reelforge_version="0.5.0",
    )


# --- overlap_fraction geometry ---------------------------------------------


def test_overlap_identity_is_one():
    assert overlap_fraction(10, 40, 10, 40) == 1.0


def test_overlap_disjoint_is_zero():
    assert overlap_fraction(10, 40, 50, 80) == 0.0


def test_overlap_partial_fraction_of_pick():
    # pick 10-40 (30s), reel 25-60 -> intersection 15s -> 0.5 of the pick
    assert overlap_fraction(10, 40, 25, 60) == 0.5


def test_overlap_reel_contains_pick():
    assert overlap_fraction(20, 30, 0, 100) == 1.0


def test_overlap_degenerate_pick_is_zero():
    assert overlap_fraction(10, 10, 0, 100) == 0.0


# --- recall_at_k -----------------------------------------------------------


def test_recall_respects_rank_order_and_k():
    picks = [LabelPick(100, 130)]
    reels = [
        _reel("a", 0, 40, 1),
        _reel("b", 40, 80, 2),
        _reel("c", 95, 135, 3),  # the only one covering the pick, at rank 3
    ]
    assert recall_at_k(picks, reels, 2) == 0.0
    assert recall_at_k(picks, reels, 3) == 1.0


def test_recall_needs_half_the_pick_covered():
    # pick 0-40; reel covers 0-19 -> 47.5% < 50% -> not recalled
    assert recall_at_k([LabelPick(0, 40)], [_reel("a", 0, 19, 1)], 3) == 0.0
    # 0-21 -> 52.5% -> recalled
    assert recall_at_k([LabelPick(0, 40)], [_reel("a", 0, 21, 1)], 3) == 1.0


def test_recall_averages_over_picks():
    picks = [LabelPick(0, 30), LabelPick(200, 230)]
    reels = [_reel("a", 0, 30, 1)]
    assert recall_at_k(picks, reels, 3) == 0.5


def test_recall_empty_picks_is_zero():
    assert recall_at_k([], [_reel("a", 0, 30, 1)], 3) == 0.0


# --- load_labels -----------------------------------------------------------


def test_load_labels_reads_json_and_skips_sample_and_malformed(tmp_path: Path):
    good = {"asset_id": "abc", "picks": [{"start_sec": 1.0, "end_sec": 2.0, "note": "x"}]}
    (tmp_path / "abc.json").write_text(json.dumps(good))
    (tmp_path / "example.json.sample").write_text(json.dumps(good))  # not *.json
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "missing_keys.json").write_text(json.dumps({"picks": "nope"}))
    labels = load_labels(tmp_path)
    assert [l.asset_id for l in labels] == ["abc"]
    assert labels[0].picks[0].note == "x"


# --- evaluate_asset --------------------------------------------------------


def test_evaluate_asset_missing_reels_json(tmp_path: Path):
    ev = evaluate_asset(AssetLabels("nope", [LabelPick(0, 10)]), tmp_path)
    assert ev.error == "no reels.json"
    assert "ERROR" in format_report([ev])


def test_evaluate_asset_happy_path(tmp_path: Path):
    asset_id = "asset1"
    wd = tmp_path / "working" / asset_id
    wd.mkdir(parents=True)
    sel = _selection(asset_id, [_reel("a", 10, 45, 1), _reel("b", 60, 95, 2)])
    (wd / "reels.json").write_text(sel.model_dump_json())
    labels = AssetLabels(asset_id, [LabelPick(12, 44), LabelPick(300, 330)])
    ev = evaluate_asset(labels, tmp_path)
    assert ev.error is None
    assert ev.recall_at[3] == 0.5
    assert ev.candidates_generated == 2
    assert ev.input_tokens == 100 and ev.output_tokens == 50
    assert not ev.resumed_zero_tokens
    report = format_report([ev])
    assert "0.50" in report and "100/50" in report


def test_evaluate_asset_flags_resumed_zero_tokens(tmp_path: Path):
    asset_id = "asset2"
    wd = tmp_path / "working" / asset_id
    wd.mkdir(parents=True)
    sel = _selection(asset_id, [_reel("a", 0, 30, 1)], tokens=(0, 0))
    (wd / "reels.json").write_text(sel.model_dump_json())
    ev = evaluate_asset(AssetLabels(asset_id, [LabelPick(0, 30)]), tmp_path)
    assert ev.resumed_zero_tokens
    assert "--resume" in format_report([ev])
