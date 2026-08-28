"""CP8: time-based overlap, greedy dedup, MMR diversity, post-refine backfill."""

from __future__ import annotations

from reelforge_core.models import RankedReel, ReelScores, SelectionConfig
from reelforge_core.reels.dedup import (
    assign_ranks_and_truncate,
    dedup,
    mmr_diversify,
    overlap_ratio,
    resolve_post_refine_overlaps,
    similarity,
)


def _reel(
    cid: str,
    start: float,
    end: float,
    overall: float,
    mood: str = "neutral",
) -> RankedReel:
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
            narrative_coherence=50,
            hook_strength=50,
            emotional_payoff=50,
            standalone_clarity=50,
        ),
        overall=overall,
        rank=0,
        suggested_mood=mood,
    )


# ---- time-based overlap geometry (non-scene-aligned bounds) ----------------


def test_overlap_identity_is_one() -> None:
    assert overlap_ratio(_reel("a", 10.3, 45.7, 10), _reel("b", 10.3, 45.7, 10)) == 1.0


def test_overlap_disjoint_is_zero() -> None:
    assert overlap_ratio(_reel("a", 0.0, 30.0, 10), _reel("b", 30.0, 60.0, 10)) == 0.0


def test_overlap_subset_is_one_using_shorter_denom() -> None:
    big = _reel("a", 0.0, 60.0, 10)
    small = _reel("b", 12.5, 42.5, 10)  # nested -> 30/30 = 1.0
    assert overlap_ratio(big, small) == 1.0


def test_overlap_partial() -> None:
    a = _reel("a", 0.0, 30.0, 10)
    b = _reel("b", 15.0, 45.0, 10)  # intersection 15 / shorter 30 = 0.5
    assert overlap_ratio(a, b) == 0.5


def test_overlap_degenerate_span_is_zero() -> None:
    assert overlap_ratio(_reel("a", 10.0, 10.0, 10), _reel("b", 0.0, 30.0, 10)) == 0.0


def test_dedup_keeps_highest_score_on_identity() -> None:
    hi = _reel("a", 0.0, 30.0, overall=90)
    lo = _reel("b", 0.0, 30.0, overall=50)
    kept, dropped = dedup([lo, hi], SelectionConfig())
    assert [r.candidate_id for r in kept] == ["a"]
    assert dropped == 1


def test_dedup_threshold_edge_strict_less_than() -> None:
    # Overlap exactly 0.5 (the default threshold): strict `<` -> DROPPED.
    a = _reel("a", 0.0, 30.0, overall=90)
    b = _reel("b", 15.0, 45.0, overall=80)
    kept, dropped = dedup([a, b], SelectionConfig())
    assert [r.candidate_id for r in kept] == ["a"]
    assert dropped == 1
    # Just under the threshold survives.
    c = _reel("c", 15.1, 45.1, overall=80)
    kept, dropped = dedup([a, c], SelectionConfig())
    assert [r.candidate_id for r in kept] == ["a", "c"]


def test_dedup_keeps_disjoint_spans() -> None:
    a = _reel("a", 0.0, 30.0, overall=90)
    b = _reel("b", 40.0, 75.0, overall=85)
    kept, dropped = dedup([a, b], SelectionConfig())
    assert [r.candidate_id for r in kept] == ["a", "b"]
    assert dropped == 0


def test_assign_ranks_and_truncate() -> None:
    reels = [
        _reel("a", 0, 30, overall=90),
        _reel("b", 40, 70, overall=80),
        _reel("c", 80, 110, overall=70),
    ]
    out = assign_ranks_and_truncate(reels, top_k=2)
    assert [r.rank for r in out] == [1, 2]
    assert [r.candidate_id for r in out] == ["a", "b"]


# ---- similarity + MMR ------------------------------------------------------


def test_similarity_jaccard_plus_mood_bonus() -> None:
    assert similarity({"jump", "snow"}, {"jump", "snow"}, "energetic", "energetic") == 1.25
    assert similarity({"jump"}, {"talk"}, "calm", "energetic") == 0.0
    assert similarity({"a", "b"}, {"b", "c"}, "calm", "calm") == 1 / 3 + 0.25
    assert similarity(set(), set(), "calm", "calm") == 0.25


def test_mmr_demotes_same_topic_duplicate() -> None:
    # Non-overlapping spans (so dedup keeps all three): two jump reels with
    # identical tags+mood, one slightly weaker crowd reel with a different
    # topic. Pure overall order: jump1, jump2, crowd. MMR pushes crowd up.
    jump1 = _reel("jump1", 0, 30, overall=90, mood="energetic")
    jump2 = _reel("jump2", 40, 70, overall=85, mood="energetic")
    crowd = _reel("crowd", 80, 110, overall=82, mood="calm")
    tags = {
        "jump1": {"snowboard", "jump", "air"},
        "jump2": {"snowboard", "jump", "air"},
        "crowd": {"crowd", "applause"},
    }
    ordered = mmr_diversify([jump1, jump2, crowd], tags, lam=8.0)
    # jump2's penalty: 8 * 1.25 = 10 -> 75; crowd unpenalized at 82.
    assert [r.candidate_id for r in ordered] == ["jump1", "crowd", "jump2"]


def test_mmr_lambda_zero_reproduces_pure_order() -> None:
    reels = [
        _reel("a", 0, 30, overall=90, mood="calm"),
        _reel("b", 40, 70, overall=85, mood="calm"),
        _reel("c", 80, 110, overall=80, mood="calm"),
    ]
    tags = {"a": {"x"}, "b": {"x"}, "c": {"y"}}
    assert [r.candidate_id for r in mmr_diversify(reels, tags, lam=0.0)] == ["a", "b", "c"]
    # With λ on, b (same tags as a: penalty 8*1.25=10 -> 75) falls behind
    # c (mood-only penalty 8*0.25=2 -> 78).
    assert [r.candidate_id for r in mmr_diversify(reels, tags, lam=8.0)] == ["a", "c", "b"]


# ---- post-refine collision + backfill --------------------------------------


def test_post_refine_collision_drops_lower_and_backfills() -> None:
    cfg = SelectionConfig()
    # After refinement, b's span slid onto a's (overlap 1.0).
    a = _reel("a", 0.0, 40.0, overall=90)
    b = _reel("b", 5.0, 35.0, overall=85)
    reserve = [_reel("r1", 100.0, 130.0, overall=70)]
    kept = resolve_post_refine_overlaps([a, b], reserve, cfg)
    assert [r.candidate_id for r in kept] == ["a", "r1"]


def test_post_refine_backfill_skips_colliding_reserve() -> None:
    cfg = SelectionConfig()
    a = _reel("a", 0.0, 40.0, overall=90)
    b = _reel("b", 5.0, 35.0, overall=85)  # collides with a
    reserve = [
        _reel("r1", 10.0, 45.0, overall=70),  # also collides with a
        _reel("r2", 100.0, 130.0, overall=65),
    ]
    kept = resolve_post_refine_overlaps([a, b], reserve, cfg)
    assert [r.candidate_id for r in kept] == ["a", "r2"]


def test_post_refine_no_collision_is_identity() -> None:
    cfg = SelectionConfig()
    a = _reel("a", 0.0, 40.0, overall=90)
    b = _reel("b", 50.0, 90.0, overall=85)
    kept = resolve_post_refine_overlaps([a, b], [_reel("r1", 100, 130, 70)], cfg)
    assert [r.candidate_id for r in kept] == ["a", "b"]
