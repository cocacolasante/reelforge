"""Speech-safe cut points: never start or end a reel mid-word.

Scene boundaries come from visual detection (PySceneDetect) and long-take
splitting; neither guarantees a boundary avoids speech. Interior boundaries
within a reel are contiguous in the source (the crossfade preserves the word),
but the reel's OUTER bounds hard-cut the audio — starting or ending mid-word
sounds broken. These helpers nudge an outer bound to the nearest word edge:

- extend outward (include the whole word) when the partial word is short
  enough — up to `max_nudge` seconds;
- otherwise retreat inward, dropping the partial word entirely.

Pure functions; used by clip extraction and mirrored by caption timing.
"""

from __future__ import annotations

from reelforge_core.models import Transcript


def flatten_words(transcript: Transcript | None) -> list[tuple[float, float]]:
    if transcript is None:
        return []
    return [(w.start, w.end) for seg in transcript.segments for w in seg.words]


def snap_start(t: float, words: list[tuple[float, float]], max_nudge: float = 0.6) -> float:
    """If `t` lands inside a word, move it to that word's start (include the
    word) when close enough, else to the word's end (drop the partial word)."""
    for ws, we in words:
        if ws < t < we:
            if t - ws <= max_nudge:
                return ws
            return we
    return t


def snap_end(t: float, words: list[tuple[float, float]], max_nudge: float = 0.6) -> float:
    """If `t` lands inside a word, move it to that word's end (include the
    word) when close enough, else to the word's start (drop the partial word)."""
    for ws, we in words:
        if ws < t < we:
            if we - t <= max_nudge:
                return we
            return ws
    return t
