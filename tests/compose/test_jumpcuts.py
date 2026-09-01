"""Edit Quality CP2: silence splitting + jump-cut shot-plan expansion."""

from __future__ import annotations

import pytest

from reelforge_core.compose.jumpcuts import (
    JUMP_CUT,
    apply_jump_cuts,
    split_on_silences,
)
from reelforge_core.models import Transcript, TranscriptSegment, TranscriptWord


def _w(start: float, end: float, word: str = "w") -> TranscriptWord:
    return TranscriptWord(start=start, end=end, word=word, probability=0.9)


def _transcript(words: list[TranscriptWord]) -> Transcript:
    return Transcript(
        language="en",
        language_probability=1.0,
        duration=words[-1].end,
        segments=[
            TranscriptSegment(
                start=words[0].start, end=words[-1].end, text="x", words=words
            )
        ],
    )


def _speech_with_gaps() -> Transcript:
    # Speech 0.2-3.0, silence 3.0-6.0 (3s), speech 6.0-9.8, silence 9.8-11.0
    # (1.2s), speech 11.0-14.0.
    words = []
    t = 0.2
    while t < 2.8:
        words.append(_w(t, t + 0.3))
        t += 0.4
    words.append(_w(2.7, 3.0))
    t = 6.0
    while t < 9.6:
        words.append(_w(t, t + 0.3))
        t += 0.4
    words.append(_w(9.5, 9.8))
    t = 11.0
    while t < 13.8:
        words.append(_w(t, t + 0.3))
        t += 0.4
    return _transcript(sorted(words, key=lambda w: w.start))


def test_split_removes_two_silences_with_pads():
    pieces = split_on_silences((0.0, 14.0), _speech_with_gaps())
    assert len(pieces) == 3
    (a0, a1), (b0, b1), (c0, c1) = pieces
    assert a0 == 0.0 and a1 == pytest.approx(3.15)  # gap start 3.0 + 0.15 pad
    assert b0 == pytest.approx(5.85) and b1 == pytest.approx(9.95)
    assert c0 == pytest.approx(10.85) and c1 == 14.0
    # Strictly increasing, disjoint.
    flat = [v for p in pieces for v in p]
    assert flat == sorted(flat)


def test_split_ignores_short_gaps():
    # 0.4s gap (>= word-gap floor 0.3 but < jump-cut min 0.6) is kept.
    words = [_w(0.0, 1.0), _w(1.4, 5.0)]
    assert split_on_silences((0.0, 5.0), _transcript(words)) == [(0.0, 5.0)]


def test_split_ignores_boundary_touching_gaps():
    # The silence starts before the shot does — outer edges are left alone.
    words = [_w(4.0, 5.0), _w(8.0, 12.0)]
    pieces = split_on_silences((4.5, 12.0), _transcript(words))
    # Gap 5.0-8.0 is inside (4.5, 12) -> split; but a gap overlapping the
    # start bound is not:
    assert len(pieces) == 2
    assert split_on_silences((6.0, 12.0), _transcript(words)) == [(6.0, 12.0)]


def test_split_refuses_fragments():
    # Cutting this silence would leave a 0.35s head (< 0.4 floor) — keep it.
    words = [_w(0.0, 0.2), _w(2.0, 6.0)]
    assert split_on_silences((0.0, 6.0), _transcript(words)) == [(0.0, 6.0)]


def test_split_noop_cases():
    assert split_on_silences((3.0, 40.0), None) == [(3.0, 40.0)]
    words = [_w(0.0, 0.3)]
    assert split_on_silences((0.0, 0.7), _transcript(words)) == [(0.0, 0.7)]


def test_apply_jump_cuts_marks_intra_shot_cuts_only():
    transcript = _speech_with_gaps()
    shots, per_cut = apply_jump_cuts(
        [(0, 0.0, 14.0), (1, 14.0, 20.0)], transcript
    )
    # Shot 0 splits into 3; shot 1 (no interior gaps in 14-20) stays whole.
    assert [idx for idx, _, _ in shots] == [0, 0, 0, 1]
    assert per_cut == [JUMP_CUT, JUMP_CUT, None]


def test_apply_jump_cuts_without_transcript_is_identity():
    shots, per_cut = apply_jump_cuts([(0, 0.0, 10.0), (1, 10.0, 20.0)], None)
    assert shots == [(0, 0.0, 10.0), (1, 10.0, 20.0)]
    assert per_cut == [None]


def test_promoted_helpers_are_public():
    from reelforge_core.analysis.segments import snap_boundary, word_gaps

    assert word_gaps(None) == []
    assert snap_boundary(5.0, 2.0, 0.0, 10.0, [], []) == 5.0
