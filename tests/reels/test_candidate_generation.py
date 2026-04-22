from __future__ import annotations

from reelforge_core.models import SelectionConfig
from reelforge_core.reels import generate_candidates

from tests.reels._fixtures import make_analysis


def _ids(candidates):
    return {c.candidate_id for c in candidates}


def test_ten_five_second_scenes_enumerate_valid_windows() -> None:
    analysis = make_analysis("a", [5.0] * 10)
    config = SelectionConfig(target_min_sec=30, target_max_sec=60, max_scenes_per_reel=12)
    cands = generate_candidates(analysis, config)
    # For 10 scenes of 5s each: windows of 6, 7, ..., 12 scenes land in [30, 60].
    # But max_scenes=12; actually available is min(12, remaining). Count by hand:
    #   length 6 → dur 30.0 → valid at start i=0..4 (5 candidates)
    #   length 7 → dur 35.0 → valid at start i=0..3 (4)
    #   length 8 → dur 40.0 → i=0..2 (3)
    #   length 9 → dur 45.0 → i=0..1 (2)
    #   length 10 → dur 50.0 → i=0 (1)
    # That's 5+4+3+2+1 = 15 candidates. (Length 11/12 can't fit within 10 scenes.)
    assert len(cands) == 15
    # All durations within [30, 60]
    for c in cands:
        assert 30.0 <= c.duration_sec <= 60.0


def test_single_long_scene_becomes_single_candidate() -> None:
    analysis = make_analysis("a", [50.0])
    config = SelectionConfig()
    cands = generate_candidates(analysis, config)
    assert len(cands) == 1
    assert cands[0].scene_indices == [0]
    assert cands[0].duration_sec == 50.0


def test_all_two_second_scenes_with_default_max_scenes_produces_nothing() -> None:
    analysis = make_analysis("a", [2.0] * 100)
    config = SelectionConfig(max_scenes_per_reel=6)  # max window 12s < 30s
    cands = generate_candidates(analysis, config)
    assert cands == []


def test_all_two_second_scenes_with_relaxed_max_produces_many() -> None:
    analysis = make_analysis("a", [2.0] * 100)
    config = SelectionConfig(max_scenes_per_reel=30)
    cands = generate_candidates(analysis, config)
    # At 2s each, windows of 15..30 scenes → 30..60s.
    # Count: 100 - 15 + 1 = 86, 100 - 16 + 1 = 85, ..., 100 - 30 + 1 = 71.
    # Sum(71..86) = (71+86)*16/2 = 1256.
    assert len(cands) == 1256
    for c in cands:
        assert 30.0 <= c.duration_sec <= 60.0


def test_all_ninety_second_scenes_produces_nothing() -> None:
    analysis = make_analysis("a", [90.0, 90.0, 90.0])
    cands = generate_candidates(analysis, SelectionConfig())
    assert cands == []


def test_empty_scene_list() -> None:
    analysis = make_analysis("a", [])
    cands = generate_candidates(analysis, SelectionConfig())
    assert cands == []


def test_candidate_ids_are_stable() -> None:
    analysis = make_analysis("asset", [20.0, 20.0, 20.0])
    cfg = SelectionConfig()
    a = generate_candidates(analysis, cfg)
    b = generate_candidates(analysis, cfg)
    assert _ids(a) == _ids(b)
