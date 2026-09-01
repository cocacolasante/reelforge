"""Jump cuts: remove dead air inside shots (pure, deterministic).

Talking-head footage is full of pauses — the editing move that makes it watch
well is cutting them out and letting the image jump. This module splits a
shot's source bounds around silences in the word timeline; the pipeline turns
each sub-bound into its own clip joined by hard cuts.

Rules:
- only inter-word gaps >= `min_gap_sec` count as removable dead air;
- `keep_pad_sec` of air is kept on each side of the cut so speech never feels
  clipped;
- gaps touching the shot's outer bounds are ignored (speech-safe snapping
  already handled the edges);
- no sub-shot shorter than `min_shot_sec` is produced — a silence whose
  removal would create one is simply kept.
"""

from __future__ import annotations

from reelforge_core.analysis.segments import word_gaps
from reelforge_core.models import Transcript

MIN_GAP_SEC = 0.6
KEEP_PAD_SEC = 0.15
MIN_SHOT_SEC = 0.4
# The hard cut used between sub-shots of one split shot.
JUMP_CUT = ("cut", 0.04)


def split_on_silences(
    bounds: tuple[float, float],
    transcript: Transcript | None,
    *,
    min_gap_sec: float = MIN_GAP_SEC,
    keep_pad_sec: float = KEEP_PAD_SEC,
    min_shot_sec: float = MIN_SHOT_SEC,
) -> list[tuple[float, float]]:
    """Split (start, end) around removable silences. Always returns at least
    the original bounds; sub-bounds are strictly increasing and disjoint."""
    start, end = bounds
    if transcript is None or end - start <= 2 * min_shot_sec:
        return [(start, end)]

    pieces: list[tuple[float, float]] = []
    cur = start
    for mid, length in sorted(word_gaps(transcript)):
        if length < min_gap_sec:
            continue
        g0 = mid - length / 2.0
        g1 = mid + length / 2.0
        if g0 <= start or g1 >= end:
            continue  # touches an outer bound — leave the edges alone
        cut_end = g0 + keep_pad_sec
        resume = g1 - keep_pad_sec
        if resume - cut_end <= 0:
            continue  # pads swallowed the gap
        if cut_end - cur < min_shot_sec or end - resume < min_shot_sec:
            continue  # would create a fragment
        pieces.append((cur, round(cut_end, 3)))
        cur = round(resume, 3)
    pieces.append((cur, end))
    return pieces


def apply_jump_cuts(
    scene_bounds: list[tuple[int, float, float]],
    transcript: Transcript | None,
    *,
    min_gap_sec: float = MIN_GAP_SEC,
) -> tuple[list[tuple[int, float, float]], list[tuple[str, float] | None]]:
    """Expand a scene-mode shot plan with jump cuts.

    Input: ordered (scene_index, in_ts, out_ts) triples. Output: the expanded
    triples plus a per-cut override list (len n-1) — `JUMP_CUT` between
    sub-shots born from one shot, `None` (reel default) between original
    shots. Pure."""
    shots: list[tuple[int, float, float]] = []
    per_cut: list[tuple[str, float] | None] = []
    for scene_idx, s, e in scene_bounds:
        pieces = split_on_silences((s, e), transcript, min_gap_sec=min_gap_sec)
        for j, (ps, pe) in enumerate(pieces):
            if shots:
                # Cut style for the boundary BEFORE this shot.
                per_cut.append(JUMP_CUT if j > 0 else None)
            shots.append((scene_idx, ps, pe))
    return shots, per_cut
