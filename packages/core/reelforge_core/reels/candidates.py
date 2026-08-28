"""Candidate generation: enumerate reel-able spans from analysis artifacts.

Selection v2 contract: a candidate is a TIME span — `start_sec`/`end_sec` are
the authoritative bounds and the identity (`candidate_id` hashes the bounds,
not the scene list). `scene_indices` records the scenes that *cover* the span
so compose can extract per-scene clips. Generators (scene today; sentence and
moment later) each produce candidates; `generate_candidates` unions them,
deduping exact bound collisions.
"""

from __future__ import annotations

import hashlib
import logging
from bisect import bisect_right
from typing import Sequence

from reelforge_core.models import AnalysisReport, ReelCandidate, Scene, SelectionConfig

log = logging.getLogger(__name__)


def _candidate_id(asset_id: str, start_sec: float, end_sec: float) -> str:
    """Identity is the time span (integer milliseconds), not the scene list —
    two generators proposing the same bounds are the same candidate."""
    raw = f"{asset_id}|{_ms(start_sec)}|{_ms(end_sec)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _ms(t: float) -> int:
    return int(round(t * 1000))


def covering_scenes(scenes: Sequence[Scene], start: float, end: float) -> list[int]:
    """Indices of scenes whose interval intersects [start, end), ascending.

    Pure; O(log n + k). Boundary-touching scenes (scene.end_sec == start or
    scene.start_sec == end) do NOT intersect the half-open span.
    """
    if end <= start or not scenes:
        return []
    starts = [s.start_sec for s in scenes]
    i = max(0, bisect_right(starts, start) - 1)
    out: list[int] = []
    for s in scenes[i:]:
        if s.end_sec <= start:
            continue
        if s.start_sec >= end:
            break
        out.append(s.index)
    return out


def generate_scene_candidates(
    analysis: AnalysisReport, config: SelectionConfig
) -> list[ReelCandidate]:
    """Enumerate contiguous scene spans with duration in [target_min_sec, target_max_sec].

    Pure function. Does no I/O. Break inner loop as soon as the span exceeds the
    max duration — any further extension can only make it longer. Returns the full
    list; ranking + dedup handle overlap.
    """
    scenes: list[Scene] = analysis.scenes
    n = len(scenes)
    out: list[ReelCandidate] = []
    # Effective bounds honor SelectionConfig.output_form: long_single widens
    # the [min, max] window around the user's target duration; short and
    # long_montage use the literal target_min/max_sec.
    min_sec = config.effective_min_sec
    max_sec = config.effective_max_sec
    max_scenes = max(1, config.effective_max_scenes)
    if n == 0:
        return out
    for i in range(n):
        upper = min(i + max_scenes, n)
        for j in range(i, upper):
            start = scenes[i].start_sec
            end = scenes[j].end_sec
            dur = end - start
            if dur > max_sec:
                break  # extending j further only makes dur larger
            if dur < min_sec:
                continue
            indices = list(range(i, j + 1))
            out.append(
                ReelCandidate(
                    candidate_id=_candidate_id(analysis.asset_id, start, end),
                    scene_indices=indices,
                    start_sec=start,
                    end_sec=end,
                    duration_sec=round(dur, 6),
                    scene_count=len(indices),
                    source="scene",
                )
            )
    return out


# Priority order for the max_candidates cap: speech-aligned spans are the
# strongest openers, so they survive truncation first.
_SOURCE_PRIORITY = ("sentence", "scene", "moment")


def generate_candidates(
    analysis: AnalysisReport, config: SelectionConfig
) -> list[ReelCandidate]:
    """Union over all candidate generators, deduping exact (start_ms, end_ms)
    collisions — first generator wins. Capped at config.max_candidates
    (sentence kept first, then scene, then moment; within a generator the
    survivors are evenly strided through time so coverage stays uniform)."""
    # Function-local import: generators import _candidate_id/covering_scenes
    # from this module, so a top-level import would be circular.
    from reelforge_core.reels.generators.moment import generate_moment_candidates
    from reelforge_core.reels.generators.sentence import generate_sentence_candidates

    generators = (
        generate_sentence_candidates,
        generate_scene_candidates,
        generate_moment_candidates,
    )
    out: list[ReelCandidate] = []
    seen: set[tuple[int, int]] = set()
    for gen in generators:
        for c in gen(analysis, config):
            key = (_ms(c.start_sec), _ms(c.end_sec))
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return _cap_candidates(out, config.max_candidates)


def _cap_candidates(
    candidates: list[ReelCandidate], cap: int
) -> list[ReelCandidate]:
    if len(candidates) <= cap:
        return candidates
    kept: list[ReelCandidate] = []
    remaining = cap
    for source in _SOURCE_PRIORITY:
        group = [c for c in candidates if c.source == source]
        if not group:
            continue
        if len(group) <= remaining:
            kept.extend(group)
            remaining -= len(group)
        else:
            if remaining > 0:
                kept.extend(_stride_sample(sorted(group, key=lambda c: c.start_sec), remaining))
            log.warning(
                "candidate cap %d: kept %d of %d %r candidates (evenly strided by time)",
                cap,
                max(0, remaining),
                len(group),
                source,
            )
            remaining = 0
    return kept


def _stride_sample(items: list[ReelCandidate], n: int) -> list[ReelCandidate]:
    """n items evenly strided across the list (endpoints included)."""
    if n >= len(items):
        return items
    if n == 1:
        return [items[0]]
    step = (len(items) - 1) / (n - 1)
    picked = {int(round(k * step)) for k in range(n)}
    return [items[i] for i in sorted(picked)]


def candidate_set_hash(candidates: Sequence[ReelCandidate]) -> str:
    """Stable hash of a candidate set — used by the ranking stamp."""
    ids = sorted(c.candidate_id for c in candidates)
    h = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()
    return h
