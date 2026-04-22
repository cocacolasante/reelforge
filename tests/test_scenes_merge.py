from __future__ import annotations

from reelforge_core.analysis.scenes import merge_short_scenes


def test_empty_returns_empty() -> None:
    assert merge_short_scenes([], 2.0) == []


def test_singleton_not_merged_even_if_short() -> None:
    # a single short scene has no neighbor to merge into
    assert merge_short_scenes([(0.0, 1.0)], 2.0) == [(0.0, 1.0)]


def test_merge_into_shorter_right_neighbor() -> None:
    # middle scene [10, 11] is 1s (< 2); left 10s vs right 5s → right shorter
    out = merge_short_scenes([(0, 10), (10, 11), (11, 16)], 2.0)
    assert out == [(0, 10), (10, 16)]


def test_merge_into_shorter_left_neighbor() -> None:
    # middle scene [10, 11] is 1s; left 5s vs right 10s → left shorter
    out = merge_short_scenes([(0, 5), (5, 6), (6, 16)], 2.0)
    assert out == [(0, 6), (6, 16)]


def test_merge_first_scene_into_right() -> None:
    out = merge_short_scenes([(0, 1), (1, 10)], 2.0)
    assert out == [(0, 10)]


def test_merge_last_scene_into_left() -> None:
    out = merge_short_scenes([(0, 10), (10, 11)], 2.0)
    assert out == [(0, 11)]


def test_no_merge_when_all_long_enough() -> None:
    out = merge_short_scenes([(0, 5), (5, 10), (10, 20)], 2.0)
    assert out == [(0, 5), (5, 10), (10, 20)]


def test_cascading_short_scenes_resolve() -> None:
    # three short scenes in a row eventually merge into a single run
    out = merge_short_scenes([(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 5.0)], 2.0)
    # final result should be one combined scene from 0 → 5
    assert out == [(0, 5.0)]
