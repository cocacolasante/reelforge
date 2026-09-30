"""Pro-editing CP12: the learning loop — labels joined to reels + scorecards,
correlation report, offline weight fit, and performer examples."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from reelforge_core.learning.dataset import collect, latest_labels
from reelforge_core.learning.fewshot import examples_block, top_examples
from reelforge_core.learning.report import correlations, spearman
from reelforge_core.learning.weights import (
    DEFAULT_WEIGHTS,
    MIN_LABELS,
    LearnedWeights,
    fit_weights,
    load_weights,
    save_weights,
)
from reelforge_core.models import ReelScores, SelectionConfig

AID = "a" * 64


def _seed(tmp_path: Path, n: int, *, driver: str = "hook_strength", seed: int = 3) -> Path:
    """n clips whose completion rises with `driver` (+ noise); reels.json,
    qa.json and a labels db laid out like /data."""
    rng = random.Random(seed)
    data = tmp_path / "data"
    wd = data / "working" / AID
    reels = []
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data / "labels.sqlite3")
    db.execute("""CREATE TABLE clip_performance_labels (reelforge_clip_id TEXT, observed_at TEXT,
                  asset_id TEXT, tenant_id TEXT, completion_rate REAL, shares_per_view REAL,
                  watch_time_pct REAL, verdict TEXT, raw TEXT, ingested_at TEXT,
                  PRIMARY KEY (reelforge_clip_id, observed_at))""")
    for i in range(n):
        cid = f"clip{i:03d}"
        scores = {k: rng.randint(30, 95) for k in DEFAULT_WEIGHTS}
        comp = 0.2 + 0.006 * scores[driver] + rng.uniform(-0.05, 0.05)
        reels.append({"candidate_id": cid, "title": f"Reel {i}", "duration_sec": 30.0 + i % 7,
                      "scores": scores, "overall": 60.0, "rank_position": 1 + i % 5,
                      "opening_description": f"opening {i}", "edit_style": "talking_head"})
        # An older observation that must lose to the newer one.
        db.execute("INSERT INTO clip_performance_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (cid, "2026-09-01", AID, None, 0.01, 0.0, 0.0, "flop", "{}", None))
        db.execute("INSERT INTO clip_performance_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (cid, "2026-09-20", AID, None, comp, comp / 20, comp * 0.9, None, "{}", None))
        rd = wd / "reels" / cid
        rd.mkdir(parents=True)
        (rd / "qa.json").write_text(json.dumps({"checks": [
            {"metric": "changes_per_min", "value": 10.0 + (scores[driver] / 10.0)},
            {"metric": "hook_latency_sec", "value": None},
        ]}))
    db.commit()
    db.close()
    (wd / "reels.json").write_text(json.dumps({"reels": reels}))
    return data


def test_latest_label_wins_and_rows_join(tmp_path):
    data = _seed(tmp_path, 12)
    labels = latest_labels(data / "labels.sqlite3")
    assert labels["clip000"]["observed_at"] == "2026-09-20"
    rows = collect(data)
    assert len(rows) == 12 and all(r.features for r in rows)
    r0 = rows[0].features
    assert {"hook_strength", "overall", "rank_position", "qa_changes_per_min"} <= set(r0)
    assert "qa_hook_latency_sec" not in r0  # None values are simply absent


def test_no_labels_is_nothing(tmp_path):
    assert collect(tmp_path) == []


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert spearman([1, 2], [1, 2]) is None


def test_the_real_driver_tops_the_report(tmp_path):
    rows = collect(_seed(tmp_path, 40))
    corr = correlations(rows, "completion_rate")
    top = [name for name, _, _ in corr[:2]]
    assert "hook_strength" in top and corr[0][2] > 0.6


def test_fit_refuses_too_few_labels(tmp_path):
    with pytest.raises(ValueError, match=str(MIN_LABELS)):
        fit_weights(collect(_seed(tmp_path, 20)))


def test_fit_moves_toward_what_predicts_completion_but_is_shrunk(tmp_path):
    lw = fit_weights(collect(_seed(tmp_path, 60, driver="emotional_payoff")))
    w = lw.weights
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-3) and all(v >= 0 for v in w.values())
    assert w["emotional_payoff"] > DEFAULT_WEIGHTS["emotional_payoff"] + 0.1
    # Shrinkage: 60 labels move at most 60/110 of the way from the defaults.
    assert w["emotional_payoff"] <= DEFAULT_WEIGHTS["emotional_payoff"] + (1 - 0.2) * 60 / 110 + 1e-3
    assert lw.version.startswith("w1-") and lw.n == 60


def test_weights_round_trip_and_bad_files_are_ignored(tmp_path):
    lw = LearnedWeights(weights={"hook_strength": 0.4, "narrative_coherence": 0.3,
                                 "emotional_payoff": 0.2, "standalone_clarity": 0.1},
                        version="w1-abc", n=MIN_LABELS, target="completion_rate")
    path = tmp_path / "learning" / "score_weights.json"
    save_weights(lw, path)
    got = load_weights(path)
    assert got.weights == lw.weights and got.version == "w1-abc"
    s = ReelScores(hook_strength=100, narrative_coherence=0, emotional_payoff=0, standalone_clarity=0)
    assert got.combine(s) == pytest.approx(40.0)
    path.write_text(json.dumps({"weights": {"hook_strength": 2.0}, "n": 99}))
    assert load_weights(path) is None
    assert load_weights(tmp_path / "missing.json") is None


def test_examples_need_enough_labels_and_pick_the_best(tmp_path):
    assert examples_block(collect(_seed(tmp_path / "few", 6))) is None
    rows = collect(_seed(tmp_path / "many", 20))
    best = top_examples(rows)
    comps = [r.targets["completion_rate"] for r in best]
    assert len(best) == 4 and comps == sorted(comps, reverse=True)
    assert comps[0] == max(r.targets["completion_rate"] for r in rows)
    block = examples_block(rows)
    assert "WHAT HAS WORKED FOR THIS CREATOR" in block and "completion" in block


# ---- wiring into ranking -----------------------------------------------------------------


def test_ranking_uses_applied_weights_and_stamps_them(tmp_path, monkeypatch):
    from reelforge_core.reels import pipeline as rp
    from reelforge_core.reels import rank
    from reelforge_core.reels.rank import _coerce_rankings, build_system_prompt
    from tests.reels.test_cp6_hook_ending import _candidate, _entry

    cfg = SelectionConfig()
    base_stamp = rp._ranking_stamp(cfg, "h")
    assert "weights_version" not in base_stamp and "fewshot" not in base_stamp

    lw = LearnedWeights(weights={"hook_strength": 1.0, "narrative_coherence": 0.0,
                                 "emotional_payoff": 0.0, "standalone_clarity": 0.0},
                        version="w1-test", n=60, target="completion_rate")
    monkeypatch.setattr(rank, "active_weights", lambda config: lw if config.learned_weights else None)
    monkeypatch.setattr(rank, "fewshot_block", lambda config: "\n\nWHAT HAS WORKED FOR THIS CREATOR\n- x")
    entry = _entry()
    entry["scores"] = {"narrative_coherence": 10, "hook_strength": 90, "emotional_payoff": 10,
                       "standalone_clarity": 10}
    (reel,) = _coerce_rankings([entry], candidate_map={"c1": _candidate()}, weights=lw)
    assert reel.overall == pytest.approx(90.0)
    stamp = rp._ranking_stamp(cfg, "h")
    assert stamp["weights_version"] == "w1-test" and stamp["fewshot"]
    assert "WHAT HAS WORKED" in build_system_prompt(cfg)
    off = SelectionConfig(learned_weights=False, fewshot=False)
    assert "weights_version" not in rp._ranking_stamp(off, "h")
