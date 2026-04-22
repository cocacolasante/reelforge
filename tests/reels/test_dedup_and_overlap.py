from __future__ import annotations

from reelforge_core.models import RankedReel, ReelScores, SelectionConfig
from reelforge_core.reels.dedup import assign_ranks_and_truncate, dedup, overlap_ratio


def _reel(cid: str, indices: list[int], overall: float) -> RankedReel:
    return RankedReel(
        candidate_id=cid,
        scene_indices=indices,
        start_sec=0.0,
        end_sec=0.0,
        duration_sec=0.0,
        title="t",
        hook="h",
        justification="j",
        scores=ReelScores(
            narrative_coherence=50,
            hook_strength=50,
            emotional_payoff=50,
            standalone_clarity=50,
        ),
        overall=overall,
        rank=0,
        suggested_mood="neutral",
    )


def test_overlap_identity_is_one() -> None:
    a = _reel("a", [0, 1, 2], 10)
    b = _reel("b", [0, 1, 2], 10)
    assert overlap_ratio(a, b) == 1.0


def test_overlap_disjoint_is_zero() -> None:
    a = _reel("a", [0, 1, 2], 10)
    b = _reel("b", [3, 4], 10)
    assert overlap_ratio(a, b) == 0.0


def test_overlap_subset_is_one_using_smaller_denom() -> None:
    big = _reel("a", [0, 1, 2, 3], 10)
    small = _reel("b", [2, 3], 10)
    # intersection=2, smaller=2 → 1.0
    assert overlap_ratio(big, small) == 1.0


def test_overlap_partial() -> None:
    a = _reel("a", [0, 1, 2, 3], 10)
    b = _reel("b", [2, 3, 4, 5], 10)
    # intersection={2,3}=2, smaller=4 → 0.5
    assert overlap_ratio(a, b) == 0.5


def test_dedup_keeps_highest_score_on_identity() -> None:
    hi = _reel("a", [0, 1, 2], overall=90)
    lo = _reel("b", [0, 1, 2], overall=50)
    kept, dropped = dedup([lo, hi], SelectionConfig())
    assert [r.candidate_id for r in kept] == ["a"]
    assert dropped == 1


def test_dedup_drops_subset_at_threshold_edge_strict_less_than() -> None:
    # With default overlap_threshold=0.5 and strict `<`, overlap=1.0 → dropped.
    big = _reel("a", [0, 1, 2, 3], overall=60)
    small = _reel("b", [2, 3], overall=80)  # higher, so kept first
    kept, dropped = dedup([big, small], SelectionConfig())
    # small kept; big has overlap=1.0 with small (smaller set = small, intersection = {2,3})
    assert [r.candidate_id for r in kept] == ["b"]
    assert dropped == 1


def test_dedup_keeps_disjoint_spans() -> None:
    a = _reel("a", [0, 1, 2], overall=90)
    b = _reel("b", [3, 4, 5], overall=85)
    kept, dropped = dedup([a, b], SelectionConfig())
    assert [r.candidate_id for r in kept] == ["a", "b"]
    assert dropped == 0


def test_assign_ranks_and_truncate() -> None:
    reels = [
        _reel("a", [0, 1], overall=90),
        _reel("b", [2, 3], overall=80),
        _reel("c", [4, 5], overall=70),
    ]
    out = assign_ranks_and_truncate(reels, top_k=2)
    assert [r.rank for r in out] == [1, 2]
    assert [r.candidate_id for r in out] == ["a", "b"]
