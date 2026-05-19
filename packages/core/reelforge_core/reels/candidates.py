"""Enumerate scene-boundary-aligned spans that fall inside the target duration window."""

from __future__ import annotations

import hashlib
from typing import Sequence

from reelforge_core.models import AnalysisReport, ReelCandidate, Scene, SelectionConfig


def _candidate_id(asset_id: str, scene_indices: Sequence[int]) -> str:
    joined = ",".join(str(i) for i in scene_indices)
    raw = f"{asset_id}|{joined}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def generate_candidates(
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
                    candidate_id=_candidate_id(analysis.asset_id, indices),
                    scene_indices=indices,
                    start_sec=start,
                    end_sec=end,
                    duration_sec=round(dur, 6),
                    scene_count=len(indices),
                )
            )
    return out


def candidate_set_hash(candidates: Sequence[ReelCandidate]) -> str:
    """Stable hash of a candidate set — used by the ranking stamp."""
    ids = sorted(c.candidate_id for c in candidates)
    h = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()
    return h
