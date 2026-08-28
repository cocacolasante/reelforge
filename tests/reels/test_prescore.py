"""CP5: prescore features, formula, shortlist, stamp invalidation, batched-path removal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.models import (
    EnergyPoint,
    LoudnessPoint,
    ReelCandidate,
    SelectionConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from reelforge_core.reels import generate_candidates, select_reels
from reelforge_core.reels.prescore import (
    PrescoreFeatures,
    compute_features,
    prescore,
    shortlist,
)

from tests.reels._fake_ranking_client import FakeRankingClient
from tests.reels._fixtures import make_analysis


def _cand(cid: str, start: float, end: float, source: str = "scene") -> ReelCandidate:
    return ReelCandidate(
        candidate_id=cid,
        scene_indices=[0],
        start_sec=start,
        end_sec=end,
        duration_sec=end - start,
        scene_count=1,
        source=source,
    )


def _features(**overrides) -> PrescoreFeatures:
    base = dict(
        starts_on_unit_boundary=False,
        ends_on_unit_boundary=False,
        starts_mid_word=False,
        ends_mid_word=False,
        speech_ratio=0.0,
        energy_peak_pos=None,
        energy_peak_z=None,
        lufs_range=0.0,
        n_scene_cuts=0,
        source="scene",
    )
    base.update(overrides)
    return PrescoreFeatures(**base)


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, client) -> None:
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **kw: client)


# ---- feature extraction ----------------------------------------------------


def _speechful_analysis(asset_id: str):
    """One 60s scene split at 30s; a sentence 'hello there.' at 5.0-6.6 and a
    word straddling t=20 (19.8-20.6)."""
    words_a = [
        TranscriptWord(start=5.0, end=5.7, word="hello", probability=0.9),
        TranscriptWord(start=5.9, end=6.6, word="there.", probability=0.9),
    ]
    words_b = [TranscriptWord(start=19.8, end=20.6, word="straddle", probability=0.9)]
    transcript = Transcript(
        language="en",
        language_probability=1.0,
        duration=60.0,
        segments=[
            TranscriptSegment(start=5.0, end=6.6, text="hello there.", words=words_a),
            TranscriptSegment(start=19.8, end=20.6, text="straddle", words=words_b),
        ],
    )
    analysis = make_analysis(asset_id, [30.0, 30.0])
    return analysis.model_copy(update={"transcript": transcript})


def test_features_detect_unit_boundary_and_mid_word_start():
    analysis = _speechful_analysis("f1")
    cands = [
        _cand("on_unit", 5.0, 40.0),  # starts exactly on the unit start
        _cand("mid_word", 20.0, 55.0),  # starts inside 'straddle' (19.8-20.6)
        _cand("clean", 10.0, 45.0),  # neither
    ]
    feats = compute_features(cands, analysis)
    assert feats["on_unit"].starts_on_unit_boundary
    assert not feats["on_unit"].starts_mid_word
    assert feats["mid_word"].starts_mid_word
    assert not feats["mid_word"].starts_on_unit_boundary
    assert not feats["clean"].starts_mid_word
    assert not feats["clean"].starts_on_unit_boundary


def test_features_energy_peak_position_and_z():
    analysis = make_analysis("f2", [60.0])
    energy = [EnergyPoint(time_sec=i + 0.5, motion=1.0, loudness_delta=0.0) for i in range(60)]
    energy[12] = EnergyPoint(time_sec=12.5, motion=80.0, loudness_delta=0.0)
    analysis = analysis.model_copy(update={"energy": energy})
    feats = compute_features([_cand("c", 10.0, 50.0)], analysis)
    f = feats["c"]
    # Peak at 12.5 in a [10, 50] span -> position 2.5/40 = 0.0625.
    assert f.energy_peak_pos == pytest.approx(0.0625)
    assert f.energy_peak_z is not None and f.energy_peak_z > 3.0


def test_features_no_energy_yields_none():
    analysis = make_analysis("f3", [60.0])
    f = compute_features([_cand("c", 10.0, 50.0)], analysis)["c"]
    assert f.energy_peak_pos is None and f.energy_peak_z is None


def test_features_scene_cuts_and_lufs_range_exclude_sentinel():
    analysis = make_analysis("f4", [10.0, 10.0, 10.0, 10.0])
    loudness = [
        LoudnessPoint(time_sec=5.5, lufs=-30.0),
        LoudnessPoint(time_sec=15.5, lufs=-12.0),
        LoudnessPoint(time_sec=25.5, lufs=-80.0),  # sentinel: excluded
    ]
    analysis = analysis.model_copy(update={"loudness": loudness})
    f = compute_features([_cand("c", 0.0, 40.0)], analysis)["c"]
    # Interior cuts at 10, 20, 30 (0.0 is the span start, not interior).
    assert f.n_scene_cuts == 3
    assert f.lufs_range == pytest.approx(18.0)  # -12 - (-30); sentinel ignored


# ---- formula ---------------------------------------------------------------


def test_prescore_mid_word_start_ranks_below_clean():
    clean = prescore(_features(starts_on_unit_boundary=True))
    dirty = prescore(_features(starts_mid_word=True))
    assert clean == 25.0
    assert dirty == -40.0
    assert clean > dirty


def test_prescore_full_formula():
    f = _features(
        starts_on_unit_boundary=True,   # +25
        ends_on_unit_boundary=True,     # +15
        speech_ratio=0.6,               # +10
        energy_peak_z=5.0,              # +10 * min(5,3) = +30
        energy_peak_pos=0.1,            # +15
        n_scene_cuts=6,                 # +5 * min(6,4) = +20
    )
    assert prescore(f) == 115.0


# ---- shortlist -------------------------------------------------------------


def test_shortlist_skips_near_duplicates_and_caps():
    cands = [
        _cand("a", 0.0, 40.0),
        _cand("b", 0.0, 42.0),  # overlap with a: 40/40 = 1.0 -> skipped
        _cand("c", 60.0, 100.0),
        _cand("d", 62.0, 100.0),  # overlap with c: 38/38 = 1.0 -> skipped
        _cand("e", 120.0, 160.0),
    ]
    # Score order a > b > c > d > e via n_scene_cuts.
    feats = {
        "a": _features(n_scene_cuts=4),
        "b": _features(n_scene_cuts=3),
        "c": _features(n_scene_cuts=2),
        "d": _features(n_scene_cuts=1),
        "e": _features(),
    }
    kept = shortlist(cands, feats, 10)
    assert [c.candidate_id for c in kept] == ["a", "c", "e"]
    assert [c.candidate_id for c in shortlist(cands, feats, 2)] == ["a", "c"]


def test_shortlist_tie_breaks_shorter_then_earlier():
    cands = [
        _cand("long", 0.0, 60.0),
        _cand("short_late", 100.0, 140.0),
        _cand("short_early", 50.0, 90.0),
    ]
    feats = {cid: _features() for cid in ("long", "short_late", "short_early")}
    kept = shortlist(cands, feats, 3)
    assert [c.candidate_id for c in kept] == ["short_early", "short_late", "long"]


# ---- pipeline integration --------------------------------------------------


async def test_pipeline_writes_prescore_json_and_ranks_shortlist_only(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("ps1", [10.0] * 8)
    config = SelectionConfig(shortlist_size=3)
    candidates = generate_candidates(analysis, config)
    assert len(candidates) > 3

    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)
    selection = await select_reels(analysis, config)

    # candidates_generated stays the full union size...
    assert selection.candidates_generated == len(candidates)
    # ...but the API call carried only the shortlist (v2 layout: one intro
    # text block, then one JSON text block per candidate).
    blocks = client.calls[0]["messages"][0]["content"]
    cand_blocks = [b for b in blocks[1:] if b["type"] == "text"]
    assert len(cand_blocks) == 3
    assert len(selection.reels) <= 3

    ps_path = isolated_data_dir / "working" / "ps1" / "prescore.json"
    rows = json.loads(ps_path.read_text())
    assert len(rows) == len(candidates)
    assert sum(1 for r in rows if r["shortlisted"]) == 3
    scores = [r["prescore"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert {"features", "source", "start_sec"} <= set(rows[0])


async def test_resume_invalidated_by_shortlist_size_change(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("ps2", [10.0] * 8)
    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)

    await select_reels(analysis, SelectionConfig(resume=True))
    assert len(client.calls) == 1
    # Same config -> cache hit.
    await select_reels(analysis, SelectionConfig(resume=True))
    assert len(client.calls) == 1
    # Different shortlist -> different candidate_hash -> fresh ranking.
    await select_reels(analysis, SelectionConfig(resume=True, shortlist_size=3))
    assert len(client.calls) == 2


async def test_resume_invalidated_by_prescore_version_bump(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("ps3", [10.0] * 8)
    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)

    await select_reels(analysis, SelectionConfig(resume=True))
    assert len(client.calls) == 1
    import reelforge_core.reels.prescore as prescore_mod

    monkeypatch.setattr(prescore_mod, "PRESCORE_VERSION", "p999")
    await select_reels(analysis, SelectionConfig(resume=True))
    assert len(client.calls) == 2


# ---- rank() truncation safety valve ---------------------------------------


async def test_rank_truncates_oversize_sets_instead_of_batching() -> None:
    from reelforge_core.reels.rank import LARGE_SET_THRESHOLD, rank

    analysis = make_analysis("big", [900.0])
    cands = [_cand(f"c{i:03d}", float(i), float(i) + 40.0) for i in range(90)]
    client = FakeRankingClient(script=[])
    result = await rank(cands, analysis, SelectionConfig(), client=client)
    assert len(client.calls) == 1  # ONE call — no batches
    blocks = client.calls[0]["messages"][0]["content"]
    cand_blocks = [json.loads(b["text"]) for b in blocks[1:] if b["type"] == "text"]
    assert len(cand_blocks) == LARGE_SET_THRESHOLD
    # First-in-order (prescore order) candidates survive.
    assert cand_blocks[0]["candidate_id"] == "c000"
    assert len(result.reels) == LARGE_SET_THRESHOLD
