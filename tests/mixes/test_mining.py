"""AI Mix CP0: moment mining + balanced cross-asset pooling."""

from __future__ import annotations

from reelforge_core.mixes.mining import (
    LONG_MIX_BOUNDS,
    SHORT_MIX_BOUNDS,
    MinedMoment,
    mine_moments,
    moment_bounds_for,
    pool_moments,
)
from reelforge_core.models import EnergyPoint, ReelCandidate
from reelforge_core.reels.prescore import PrescoreFeatures

from tests.reels._fixtures import make_analysis


def _moment(aid: str, cid: str, start: float, score: float) -> MinedMoment:
    cand = ReelCandidate(
        candidate_id=cid,
        scene_indices=[0],
        start_sec=start,
        end_sec=start + 4.0,
        duration_sec=4.0,
        scene_count=1,
    )
    feats = PrescoreFeatures(
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
    return MinedMoment(asset_id=aid, candidate=cand, features=feats, score=score)


def test_moment_bounds_scale_with_target():
    assert moment_bounds_for(30) == SHORT_MIX_BOUNDS
    assert moment_bounds_for(90) == SHORT_MIX_BOUNDS
    assert moment_bounds_for(91) == LONG_MIX_BOUNDS
    assert moment_bounds_for(300) == LONG_MIX_BOUNDS


def test_mine_moments_produces_short_scored_deduped_spans():
    # 20 scenes of 3s: plenty of 2-8s windows.
    analysis = make_analysis("mineA", [3.0] * 20)
    moments = mine_moments(analysis, SHORT_MIX_BOUNDS)
    assert moments
    for m in moments:
        assert SHORT_MIX_BOUNDS[0] <= m.candidate.duration_sec <= SHORT_MIX_BOUNDS[1]
        assert m.moment_id == m.candidate.candidate_id
    # Best-first and deduped: no pair overlaps > 0.85 of the shorter.
    scores = [m.score for m in moments]
    assert scores == sorted(scores, reverse=True)
    for i, a in enumerate(moments):
        for b in moments[i + 1 :]:
            inter = min(a.candidate.end_sec, b.candidate.end_sec) - max(
                a.candidate.start_sec, b.candidate.start_sec
            )
            shorter = min(a.candidate.duration_sec, b.candidate.duration_sec)
            assert inter / shorter <= 0.85 + 1e-9


def test_mine_moments_uses_energy_for_action_footage():
    # One long silent scene + an energy spike: the moment generator supplies
    # windows even though scene/sentence generators have nothing short.
    analysis = make_analysis("mineB", [60.0], with_audio=False)
    energy = [EnergyPoint(time_sec=i + 0.5, motion=1.0, loudness_delta=0.0) for i in range(60)]
    energy[30] = EnergyPoint(time_sec=30.5, motion=80.0, loudness_delta=0.0)
    analysis = analysis.model_copy(update={"energy": energy})
    moments = mine_moments(analysis, SHORT_MIX_BOUNDS)
    assert moments
    assert any(m.candidate.source == "moment" for m in moments)


def test_mine_moments_empty_when_nothing_fits():
    analysis = make_analysis("mineC", [1.0], with_audio=False)  # 1s asset
    assert mine_moments(analysis, SHORT_MIX_BOUNDS) == []


def test_pool_round_robin_is_balanced_and_capped():
    per_asset = {
        "b" * 8: [_moment("b" * 8, f"b{i:03d}", i * 10.0, 100 - i) for i in range(40)],
        "a" * 8: [_moment("a" * 8, f"a{i:03d}", i * 10.0, 90 - i) for i in range(5)],
        "c" * 8: [_moment("c" * 8, f"c{i:03d}", i * 10.0, 80 - i) for i in range(40)],
    }
    pool = pool_moments(per_asset, cap=20)
    assert len(pool) == 20
    counts = {}
    for m in pool:
        counts[m.asset_id] = counts.get(m.asset_id, 0) + 1
    # The short asset contributes everything it has; the two long ones split
    # the rest near-evenly instead of the strongest clip monopolizing.
    assert counts["a" * 8] == 5
    assert abs(counts["b" * 8] - counts["c" * 8]) <= 1
    assert counts["b" * 8] >= 7
    # Within one asset, order is preserved best-first.
    b_scores = [m.score for m in pool if m.asset_id == "b" * 8]
    assert b_scores == sorted(b_scores, reverse=True)


def test_pool_deterministic_order():
    per_asset = {
        "zz": [_moment("zz", "z1", 0.0, 50)],
        "aa": [_moment("aa", "a1", 0.0, 10)],
    }
    pool = pool_moments(per_asset, cap=10)
    # Round-robin iterates assets in sorted-id order regardless of scores.
    assert [m.asset_id for m in pool] == ["aa", "zz"]


def test_pool_skips_empty_assets():
    per_asset = {"aa": [], "bb": [_moment("bb", "b1", 0.0, 10)]}
    pool = pool_moments(per_asset)
    assert [m.moment_id for m in pool] == ["b1"]
