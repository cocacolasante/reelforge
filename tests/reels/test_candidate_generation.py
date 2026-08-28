from __future__ import annotations

from reelforge_core.models import SelectionConfig
from reelforge_core.reels import generate_candidates
from reelforge_core.reels.candidates import generate_scene_candidates

from tests.reels._fixtures import make_analysis


def _ids(candidates):
    return {c.candidate_id for c in candidates}


def test_ten_five_second_scenes_enumerate_valid_windows() -> None:
    analysis = make_analysis("a", [5.0] * 10)
    config = SelectionConfig(target_min_sec=30, target_max_sec=60, max_scenes_per_reel=12)
    cands = generate_scene_candidates(analysis, config)
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
    cands = generate_scene_candidates(analysis, config)
    assert len(cands) == 1
    assert cands[0].scene_indices == [0]
    assert cands[0].duration_sec == 50.0


def test_all_two_second_scenes_with_max_six_produces_nothing() -> None:
    # The old pre-v2 default of 6 starved fast-cut footage: max window 12s < 30s.
    analysis = make_analysis("a", [2.0] * 100)
    config = SelectionConfig(max_scenes_per_reel=6)
    cands = generate_scene_candidates(analysis, config)
    assert cands == []


def test_all_two_second_scenes_with_new_default_produces_candidates() -> None:
    # Default max_scenes_per_reel=40: windows of 15..30 scenes land in [30, 60]s
    # (the enumerator breaks on dur > 60 before the scene cap bites).
    # Count: sum over length L in 15..30 of (100 - L + 1) = sum(71..86) = 1256.
    analysis = make_analysis("a", [2.0] * 100)
    cands = generate_scene_candidates(analysis, SelectionConfig())
    assert len(cands) == 1256
    for c in cands:
        assert 30.0 <= c.duration_sec <= 60.0


def test_all_two_second_scenes_with_relaxed_max_produces_many() -> None:
    analysis = make_analysis("a", [2.0] * 100)
    config = SelectionConfig(max_scenes_per_reel=30)
    cands = generate_scene_candidates(analysis, config)
    # At 2s each, windows of 15..30 scenes → 30..60s.
    # Count: 100 - 15 + 1 = 86, 100 - 16 + 1 = 85, ..., 100 - 30 + 1 = 71.
    # Sum(71..86) = (71+86)*16/2 = 1256.
    assert len(cands) == 1256
    for c in cands:
        assert 30.0 <= c.duration_sec <= 60.0


def test_all_ninety_second_scenes_produces_nothing() -> None:
    analysis = make_analysis("a", [90.0, 90.0, 90.0])
    cands = generate_scene_candidates(analysis, SelectionConfig())
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


# ---- Selection v2: time-based ids + covering scenes ------------------------


def test_candidate_id_hashes_bounds_not_scene_list() -> None:
    from reelforge_core.reels.candidates import _candidate_id

    assert _candidate_id("a", 1.0, 40.0) == _candidate_id("a", 1.0, 40.0)
    assert _candidate_id("a", 1.0, 40.0) != _candidate_id("b", 1.0, 40.0)
    assert _candidate_id("a", 1.0, 40.0) != _candidate_id("a", 1.0, 40.5)
    # Integer-millisecond quantization: sub-ms float noise doesn't change the id.
    assert _candidate_id("a", 1.0004, 40.0) == _candidate_id("a", 1.0, 40.0)


def test_scene_candidates_carry_source_and_time_ids() -> None:
    analysis = make_analysis("a", [20.0, 20.0, 20.0])
    cands = generate_candidates(analysis, SelectionConfig())
    assert cands
    for c in cands:
        assert c.source == "scene"
    # No duplicate (start, end) spans survive the union.
    spans = [(c.start_sec, c.end_sec) for c in cands]
    assert len(spans) == len(set(spans))


def test_covering_scenes_span_inside_one_scene() -> None:
    from reelforge_core.reels.candidates import covering_scenes

    scenes = make_analysis("a", [10.0] * 5).scenes  # 0-10, 10-20, ..., 40-50
    assert covering_scenes(scenes, 12.0, 18.0) == [1]


def test_covering_scenes_span_straddles_three() -> None:
    from reelforge_core.reels.candidates import covering_scenes

    scenes = make_analysis("a", [10.0] * 5).scenes
    assert covering_scenes(scenes, 12.0, 35.0) == [1, 2, 3]


def test_covering_scenes_boundary_touch_excluded() -> None:
    from reelforge_core.reels.candidates import covering_scenes

    scenes = make_analysis("a", [10.0] * 5).scenes
    # Half-open span [10, 20): scene 0 ends exactly at 10 (out), scene 2
    # starts exactly at 20 (out).
    assert covering_scenes(scenes, 10.0, 20.0) == [1]


def test_covering_scenes_degenerate_and_empty() -> None:
    from reelforge_core.reels.candidates import covering_scenes

    scenes = make_analysis("a", [10.0] * 5).scenes
    assert covering_scenes(scenes, 18.0, 18.0) == []
    assert covering_scenes([], 0.0, 10.0) == []
