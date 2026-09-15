"""Jump cuts: remove dead air inside shots (pure, deterministic).

Talking-head footage is full of pauses — the editing move that makes it watch
well is cutting them out and letting the image jump. This module splits a
shot's source bounds around silences; the pipeline turns each sub-bound into
its own clip joined by hard cuts.

Two sources of truth for "silence":
- the AUDIO, via a compose/silence.SpeechEnvelope of the analysis audio —
  preferred. Removable dead air is a stretch measured silent for at least
  AUDIO_MIN_GAP_SEC, with AUDIO_KEEP_PAD_SEC of it kept on each side.
  Measuring the audio fixed two live failures (2026-09-14): cuts clipping the
  last word of a phrase (Whisper word ends run up to 0.4s early) and pauses
  surviving because the transcript mis-timed them. A silent stretch that
  would swallow a whole transcribed word is kept (speech too quiet for the
  threshold).
- the TRANSCRIPT's inter-word gaps — the fallback when there is no audio.

Rules shared by both:
- no sub-shot shorter than `min_shot_sec` is produced — a silence whose
  removal would create one is simply kept;
- silences touching a shot's outer bounds are left alone unless the caller
  asks for `trim_edges` (audio only), which trims that dead air too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reelforge_core.analysis.segments import word_gaps
from reelforge_core.models import Transcript

if TYPE_CHECKING:
    from reelforge_core.compose.silence import SpeechEnvelope

MIN_GAP_SEC = 0.6
KEEP_PAD_SEC = 0.15
MIN_SHOT_SEC = 0.4
# Measured silence is exact, so shorter pauses count and less air is kept.
AUDIO_MIN_GAP_SEC = 0.45
AUDIO_KEEP_PAD_SEC = 0.1
# Scene bounds touching within this are one continuous span.
CONTIGUOUS_EPS = 1e-3
# The hard cut used between sub-shots of one split shot.
JUMP_CUT = ("cut", 0.04)


def split_on_silences(
    bounds: tuple[float, float],
    transcript: Transcript | None,
    *,
    envelope: "SpeechEnvelope | None" = None,
    trim_edges: bool = False,
    min_gap_sec: float = MIN_GAP_SEC,
    keep_pad_sec: float = KEEP_PAD_SEC,
    min_shot_sec: float = MIN_SHOT_SEC,
) -> list[tuple[float, float]]:
    """Split (start, end) around removable silences. Always returns at least
    one piece; pieces are strictly increasing and disjoint."""
    start, end = bounds
    if end - start <= 2 * min_shot_sec:
        return [(start, end)]
    if envelope is not None:
        return _split_on_audio(
            bounds, transcript, envelope, trim_edges=trim_edges, min_shot_sec=min_shot_sec
        )
    if transcript is None:
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


def _split_on_audio(
    bounds: tuple[float, float],
    transcript: Transcript | None,
    envelope: "SpeechEnvelope",
    *,
    trim_edges: bool,
    min_shot_sec: float,
) -> list[tuple[float, float]]:
    start, end = bounds
    words = [
        (w.start, w.end)
        for seg in (transcript.segments if transcript is not None else [])
        for w in seg.words
        if w.end > start and w.start < end
    ]
    pad = AUDIO_KEEP_PAD_SEC
    lo, hi = start, end
    interior: list[tuple[float, float]] = []
    for s, e in envelope.silent_runs(start, end, AUDIO_MIN_GAP_SEC):
        if any(s <= ws and we <= e for ws, we in words):
            continue  # would swallow a whole transcribed word: quiet speech
        touches_start = s <= start + 1e-6
        touches_end = e >= end - 1e-6
        if touches_start and touches_end:
            continue  # the whole span is silent — not ours to delete
        if touches_start or touches_end:
            if trim_edges and touches_start:
                lo = round(e - pad, 3)
            elif trim_edges:
                hi = round(s + pad, 3)
            continue
        interior.append((s, e))
    if hi - lo < min_shot_sec:
        lo, hi = start, end

    pieces: list[tuple[float, float]] = []
    cur = lo
    for s, e in interior:
        cut_end = round(s + pad, 3)
        resume = round(e - pad, 3)
        if cut_end <= cur or resume >= hi or resume <= cut_end:
            continue
        if cut_end - cur < min_shot_sec or hi - resume < min_shot_sec:
            continue  # would create a fragment
        pieces.append((cur, cut_end))
        cur = resume
    pieces.append((cur, hi))
    return pieces


def apply_jump_cuts(
    scene_bounds: list[tuple[int, float, float]],
    transcript: Transcript | None,
    *,
    envelope: "SpeechEnvelope | None" = None,
    min_gap_sec: float = MIN_GAP_SEC,
) -> tuple[list[tuple[int, float, float]], list[tuple[str, float] | None]]:
    """Expand a scene-mode shot plan with jump cuts.

    Input: ordered (scene_index, in_ts, out_ts) triples. Output: the expanded
    triples plus a per-cut override list (len n-1) — `JUMP_CUT` between
    sub-shots born from one span, `None` (reel default) elsewhere. With an
    `envelope`, contiguous shots are split as ONE span (a pause straddling a
    scene boundary used to survive), its outer dead air is trimmed, and each
    piece keeps the scene index it starts in. Pure."""
    if envelope is None:
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

    spans: list[tuple[float, float, list[tuple[int, float, float]]]] = []
    for scene_idx, s, e in scene_bounds:
        if spans and abs(s - spans[-1][1]) <= CONTIGUOUS_EPS:
            span_start, _, members = spans[-1]
            spans[-1] = (span_start, e, members + [(scene_idx, s, e)])
        else:
            spans.append((s, e, [(scene_idx, s, e)]))

    shots = []
    per_cut = []
    for span_start, span_end, members in spans:
        pieces = split_on_silences(
            (span_start, span_end), transcript, envelope=envelope, trim_edges=True
        )
        for j, (ps, pe) in enumerate(pieces):
            if shots:
                per_cut.append(JUMP_CUT if j > 0 else None)
            scene_idx = next((idx for idx, s, e in members if s <= ps < e), members[-1][0])
            shots.append((scene_idx, ps, pe))
    return shots, per_cut
