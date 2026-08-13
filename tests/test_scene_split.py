"""Unit tests for analysis/segments.py — long-take splitting."""

from __future__ import annotations

from reelforge_core.analysis.segments import split_long_scenes
from reelforge_core.models import (
    LoudnessPoint,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)


def _transcript_with_gap(gap_start: float, gap_end: float) -> Transcript:
    """Continuous speech 0..120s except one silent gap."""
    words: list[TranscriptWord] = []
    t = 0.0
    while t < 120.0:
        word_end = min(t + 0.4, 120.0)
        if not (gap_start <= t < gap_end):
            words.append(
                TranscriptWord(start=t, end=word_end, word="w", probability=0.99)
            )
        t += 0.5
    seg = TranscriptSegment(start=0.0, end=120.0, text="...", words=words)
    return Transcript(
        language="en", language_probability=1.0, duration=120.0, segments=[seg]
    )


def _flat_loudness(duration: float, dip_at: float | None = None) -> list[LoudnessPoint]:
    points = []
    for t in range(int(duration)):
        lufs = -18.0
        if dip_at is not None and t == int(dip_at):
            lufs = -55.0
        points.append(LoudnessPoint(time_sec=float(t), lufs=lufs))
    return points


def test_short_scenes_pass_through_unchanged():
    intervals = [(0.0, 30.0), (30.0, 44.9)]
    out = split_long_scenes(intervals, None, [], max_scene_sec=45.0, target_sec=40.0)
    assert out == intervals


def test_long_scene_split_into_pieces_under_max():
    out = split_long_scenes([(0.0, 111.1)], None, [], max_scene_sec=45.0, target_sec=40.0)
    assert len(out) == 3
    for start, end in out:
        assert end - start <= 45.0
    # Continuous coverage, strictly increasing
    assert out[0][0] == 0.0
    assert out[-1][1] == 111.1
    for (a_start, a_end), (b_start, b_end) in zip(out, out[1:]):
        assert a_end == b_start
        assert a_end > a_start


def test_split_is_idempotent():
    once = split_long_scenes([(0.0, 200.0)], None, [], max_scene_sec=45.0, target_sec=40.0)
    twice = split_long_scenes(once, None, [], max_scene_sec=45.0, target_sec=40.0)
    assert once == twice


def test_snaps_to_speech_pause():
    # 120s scene, target 40 -> ideal boundaries at 40 and 80.
    # Put a clear speech pause at 42.5; boundary should snap into it.
    transcript = _transcript_with_gap(41.5, 43.5)
    out = split_long_scenes(
        [(0.0, 120.0)], transcript, [], max_scene_sec=45.0, target_sec=40.0
    )
    boundaries = [end for _, end in out[:-1]]
    assert any(41.5 <= b <= 43.5 for b in boundaries)


def test_snaps_to_loudness_dip_when_no_transcript():
    # Quiet second at t=38 near the ideal 40s boundary.
    loudness = _flat_loudness(120.0, dip_at=38.0)
    out = split_long_scenes(
        [(0.0, 120.0)], None, loudness, max_scene_sec=45.0, target_sec=40.0
    )
    boundaries = [end for _, end in out[:-1]]
    assert any(abs(b - 38.5) < 0.01 for b in boundaries)


def test_falls_back_to_even_grid():
    out = split_long_scenes([(0.0, 120.0)], None, [], max_scene_sec=45.0, target_sec=40.0)
    assert len(out) == 3
    boundaries = [end for _, end in out[:-1]]
    assert boundaries == [40.0, 80.0]


def test_mixed_short_and_long_scenes():
    intervals = [(0.0, 111.1), (111.1, 119.2)]
    out = split_long_scenes(intervals, None, [], max_scene_sec=45.0, target_sec=40.0)
    # Long first scene split; short tail preserved.
    assert out[-1] == (111.1, 119.2)
    assert len(out) == 4
    assert all(end - start <= 45.0 for start, end in out)


def test_real_world_gopro_case_yields_candidates():
    """The exact live failure: 119s clip, scenes (0->111.1) + (111.1->119.2),
    target reel window 30-60s. After splitting there must be at least one
    contiguous scene span whose duration lands in [30, 60]."""
    intervals = [(0.0, 111.1), (111.1, 119.2)]
    out = split_long_scenes(intervals, None, [], max_scene_sec=45.0, target_sec=40.0)
    spans = []
    for i in range(len(out)):
        for j in range(i, len(out)):
            dur = out[j][1] - out[i][0]
            if 30.0 <= dur <= 60.0:
                spans.append((out[i][0], out[j][1]))
    assert spans, f"no 30-60s span in {out}"
