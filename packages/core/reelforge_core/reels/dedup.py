"""Overlap dedup + MMR diversity + final ordering for ranked reels (pure).

v2: overlap is TIME-based (candidate bounds are the identity; scene sets are
just coverage), and after the overlap pass an MMR re-rank pushes same-topic
near-duplicates down so the final list isn't five takes of one moment.
"""

from __future__ import annotations

from reelforge_core.models import RankedReel, SelectionConfig

# Similarity bonus when two reels share a suggested mood (on top of the
# scene-tag Jaccard, which is in [0, 1]).
SAME_MOOD_BONUS = 0.25


def overlap_ratio(a: RankedReel, b: RankedReel) -> float:
    """Time intersection divided by the SHORTER reel's duration.

    Using the shorter duration as denominator means a reel nested inside a
    longer one always reports 100% overlap, which is what we want —
    near-duplicates dominated by a shorter reel embedded in a longer one
    should not both survive. Degenerate spans report 0.0.
    """
    inter = min(a.end_sec, b.end_sec) - max(a.start_sec, b.start_sec)
    shorter = min(a.end_sec - a.start_sec, b.end_sec - b.start_sec)
    if shorter <= 0:
        return 0.0
    return max(0.0, inter) / shorter


def dedup(
    ranked: list[RankedReel], config: SelectionConfig
) -> tuple[list[RankedReel], int]:
    """Return `(kept, dropped_count)`. `kept` is ordered by `overall` descending.

    Edge behavior: strict `<` — a reel with overlap exactly equal to the
    threshold is DROPPED. Document this in the caller if users are tuning the
    threshold.
    """
    kept: list[RankedReel] = []
    dropped = 0
    for reel in sorted(ranked, key=lambda r: r.overall, reverse=True):
        if all(overlap_ratio(reel, k) < config.overlap_threshold for k in kept):
            kept.append(reel)
        else:
            dropped += 1
    return kept, dropped


def similarity(
    tags_a: set[str], tags_b: set[str], mood_a: str, mood_b: str
) -> float:
    """Topic similarity: Jaccard over scene-tag sets + a same-mood bonus."""
    union = tags_a | tags_b
    j = len(tags_a & tags_b) / len(union) if union else 0.0
    return j + (SAME_MOOD_BONUS if mood_a == mood_b else 0.0)


def mmr_diversify(
    reels: list[RankedReel],
    tag_sets: dict[str, set[str]],
    lam: float,
) -> list[RankedReel]:
    """Re-rank with maximal marginal relevance:
    `score = overall − λ · max_sim(reel, already_selected)`.

    λ is in overall-score points; λ=0 reproduces the pure overall order.
    Deterministic: ties resolve to the earlier reel in the incoming
    (overall-desc) order.
    """
    if lam <= 0 or len(reels) <= 1:
        return list(reels)
    remaining = list(reels)
    selected: list[RankedReel] = []
    while remaining:
        best = max(
            remaining,
            key=lambda r: r.overall
            - lam
            * max(
                (
                    similarity(
                        tag_sets.get(r.candidate_id, set()),
                        tag_sets.get(s.candidate_id, set()),
                        r.suggested_mood,
                        s.suggested_mood,
                    )
                    for s in selected
                ),
                default=0.0,
            ),
        )
        selected.append(best)
        remaining.remove(best)
    return selected


def resolve_post_refine_overlaps(
    topk: list[RankedReel],
    reserve: list[RankedReel],
    config: SelectionConfig,
) -> list[RankedReel]:
    """Refined edges can newly collide: greedily keep the higher-ordered reel
    of any colliding pair, then backfill open slots from `reserve` (the
    post-MMR reels that missed the initial cut, unrefined) — skipping any
    backfill that itself collides. Pure."""
    kept: list[RankedReel] = []
    for reel in topk:
        if all(overlap_ratio(reel, k) < config.overlap_threshold for k in kept):
            kept.append(reel)
    for reel in reserve:
        if len(kept) >= len(topk):
            break
        if all(overlap_ratio(reel, k) < config.overlap_threshold for k in kept):
            kept.append(reel)
    return kept


def assign_ranks_and_truncate(
    kept: list[RankedReel], top_k: int
) -> list[RankedReel]:
    """Re-emit each reel with its final 1-indexed rank, truncated to top_k."""
    out: list[RankedReel] = []
    for idx, reel in enumerate(kept[:top_k], start=1):
        out.append(reel.model_copy(update={"rank": idx}))
    return out
