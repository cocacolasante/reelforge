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


# CP7: what the reels SAY counts too — two reels of one speaker share scene
# tags but may make different points, and two made of different scenes may
# repeat the same point.
TEXT_WEIGHT = 0.5


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def similarity(
    tags_a: set[str],
    tags_b: set[str],
    mood_a: str,
    mood_b: str,
    words_a: set[str] | None = None,
    words_b: set[str] | None = None,
) -> float:
    """Topic similarity: Jaccard over scene-tag sets + a same-mood bonus +
    TEXT_WEIGHT x Jaccard over the reels' content words (when given)."""
    sim = _jaccard(tags_a, tags_b) + (SAME_MOOD_BONUS if mood_a == mood_b else 0.0)
    if words_a is not None and words_b is not None:
        sim += TEXT_WEIGHT * _jaccard(words_a, words_b)
    return sim


def content_words(transcript, start: float, end: float) -> set[str]:
    """Lower-cased content words spoken inside [start, end]: no stop words,
    nothing shorter than 4 letters. Pure."""
    from reelforge_core.compose.keywords import _STOP

    out: set[str] = set()
    if transcript is None:
        return out
    for seg in transcript.segments:
        if seg.end < start or seg.start > end:
            continue
        for w in seg.words:
            if start <= (w.start + w.end) / 2.0 <= end:
                token = "".join(ch for ch in w.word.lower() if ch.isalnum() or ch == "'")
                if len(token) >= 4 and token not in _STOP:
                    out.add(token)
    return out


def mmr_diversify(
    reels: list[RankedReel],
    tag_sets: dict[str, set[str]],
    lam: float,
    word_sets: dict[str, set[str]] | None = None,
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
                        word_sets.get(r.candidate_id) if word_sets is not None else None,
                        word_sets.get(s.candidate_id) if word_sets is not None else None,
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


def enforce_clean_edges(
    topk: list[RankedReel],
    reserve: list[RankedReel],
    config: SelectionConfig,
    events: list,
    duration: float,
) -> list[RankedReel]:
    """Final gate: a reel whose edge still cuts an action event (the guard
    couldn't fix it and refinement didn't) is dropped, and open slots are
    backfilled from `reserve` with clean, non-colliding reels. When nothing
    clean exists at all the list comes back unchanged — some reels beat none.
    Pure."""
    from reelforge_core.reels.events import edge_ok

    if not events:
        return topk

    def _clean(r: RankedReel) -> bool:
        return edge_ok(r.start_sec, "start", events, duration) and edge_ok(
            r.end_sec, "end", events, duration
        )

    kept = [r for r in topk if _clean(r)]
    kept_ids = {r.candidate_id for r in kept}
    for reel in reserve:
        if len(kept) >= len(topk):
            break
        if reel.candidate_id in kept_ids or not _clean(reel):
            continue
        if all(overlap_ratio(reel, k) < config.overlap_threshold for k in kept):
            kept.append(reel)
            kept_ids.add(reel.candidate_id)
    return kept if kept else topk


def assign_ranks_and_truncate(
    kept: list[RankedReel], top_k: int
) -> list[RankedReel]:
    """Re-emit each reel with its final 1-indexed rank, truncated to top_k."""
    out: list[RankedReel] = []
    for idx, reel in enumerate(kept[:top_k], start=1):
        out.append(reel.model_copy(update={"rank": idx}))
    return out
