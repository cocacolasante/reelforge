"""Split over-long scenes into sub-scenes at natural break points.

Raw unedited footage (GoPro runs, talking-head recordings, screen captures)
often has few hard cuts, so PySceneDetect returns scenes far longer than any
reel. Candidate enumeration composes whole scenes, so a single scene longer
than the max reel duration makes the footage yield zero candidates. This
module splits those scenes at the most natural nearby break:

1. a pause in speech (gap between transcript words), else
2. a dip in loudness (quietest 1-second bin), else
3. an even grid.

Pure functions — no I/O. The pipeline re-extracts thumbnails and rewrites
scenes.json after splitting. `word_gaps` and `snap_boundary` are public:
compose-side jump cuts (compose/jumpcuts.py) and the style planner reuse them.
"""

from __future__ import annotations

from math import ceil

from reelforge_core.models import LoudnessPoint, Transcript

# A scene longer than this gets split.
DEFAULT_MAX_SCENE_SEC = 45.0
# Aim for pieces of roughly this length; the actual piece length is
# duration / ceil(duration / target), so pieces land in (target/2, target].
DEFAULT_SPLIT_TARGET_SEC = 40.0
# Word gaps shorter than this aren't treated as speech pauses.
MIN_SPEECH_GAP_SEC = 0.3


def word_gaps(transcript: Transcript | None) -> list[tuple[float, float]]:
    """(gap_midpoint, gap_length) for every inter-word pause >= MIN_SPEECH_GAP_SEC."""
    if transcript is None:
        return []
    words = [w for seg in transcript.segments for w in seg.words]
    gaps: list[tuple[float, float]] = []
    for a, b in zip(words, words[1:]):
        gap = b.start - a.end
        if gap >= MIN_SPEECH_GAP_SEC:
            gaps.append(((a.end + b.start) / 2.0, gap))
    return gaps


def snap_boundary(
    ideal: float,
    window: float,
    lo: float,
    hi: float,
    gaps: list[tuple[float, float]],
    loudness: list[LoudnessPoint],
) -> float:
    """Pick the most natural cut point near `ideal`, clamped to (lo, hi)."""
    w_lo = max(lo, ideal - window)
    w_hi = min(hi, ideal + window)

    # 1. Speech pause: prefer long gaps close to the ideal point.
    best: tuple[float, float] | None = None  # (score, position)
    for mid, gap in gaps:
        if w_lo <= mid <= w_hi:
            score = gap - 0.15 * abs(mid - ideal)
            if best is None or score > best[0]:
                best = (score, mid)
    if best is not None:
        return best[1]

    # 2. Loudness dip: the quietest 1-second bin in the window.
    quietest: tuple[float, float] | None = None  # (lufs, bin_center)
    for point in loudness:
        center = point.time_sec + 0.5
        if w_lo <= center <= w_hi:
            if quietest is None or point.lufs < quietest[0]:
                quietest = (point.lufs, center)
    if quietest is not None:
        return quietest[1]

    # 3. Even grid.
    return ideal


def split_long_scenes(
    intervals: list[tuple[float, float]],
    transcript: Transcript | None,
    loudness: list[LoudnessPoint],
    max_scene_sec: float = DEFAULT_MAX_SCENE_SEC,
    target_sec: float = DEFAULT_SPLIT_TARGET_SEC,
) -> list[tuple[float, float]]:
    """Return intervals with every piece <= max_scene_sec.

    Idempotent: output intervals are all short enough that a second pass
    returns them unchanged. Boundaries are strictly increasing — snap windows
    are capped at 35% of the piece length so adjacent boundaries can't cross.
    """
    gaps = word_gaps(transcript)
    out: list[tuple[float, float]] = []
    for start, end in intervals:
        dur = end - start
        if dur <= max_scene_sec:
            out.append((start, end))
            continue
        parts = max(2, ceil(dur / target_sec))
        part_len = dur / parts
        # Cap the snap window three ways: absolute, proportional (so adjacent
        # boundaries can't cross), and so that a piece stretched by both of
        # its boundaries drifting apart still stays <= max_scene_sec.
        window = min(12.0, part_len * 0.35, (max_scene_sec - part_len) / 2.0)
        boundaries = [start]
        for i in range(1, parts):
            ideal = start + i * part_len
            lo = boundaries[-1]
            boundaries.append(snap_boundary(ideal, window, lo, end, gaps, loudness))
        boundaries.append(end)
        for a, b in zip(boundaries, boundaries[1:]):
            out.append((a, b))
    return out
