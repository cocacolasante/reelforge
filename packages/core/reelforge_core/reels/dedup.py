"""Greedy overlap-aware dedup. Runs on ranked reels; preserves the higher-scored
reel on conflict and drops anything that overlaps an already-kept reel at or above
the threshold."""

from __future__ import annotations

from reelforge_core.models import RankedReel, SelectionConfig


def overlap_ratio(a: RankedReel, b: RankedReel) -> float:
    """Intersection divided by the smaller scene-index set.

    Using the smaller set as denominator means a subset always reports 100%
    overlap, which is what we want — near-duplicates dominated by a shorter reel
    embedded in a longer one should not both survive.
    """
    a_set = set(a.scene_indices)
    b_set = set(b.scene_indices)
    if not a_set or not b_set:
        return 0.0
    intersection = a_set & b_set
    smaller = min(len(a_set), len(b_set))
    return len(intersection) / smaller if smaller else 0.0


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


def assign_ranks_and_truncate(
    kept: list[RankedReel], top_k: int
) -> list[RankedReel]:
    """Re-emit each reel with its final 1-indexed rank, truncated to top_k."""
    out: list[RankedReel] = []
    for idx, reel in enumerate(kept[:top_k], start=1):
        out.append(reel.model_copy(update={"rank": idx}))
    return out
