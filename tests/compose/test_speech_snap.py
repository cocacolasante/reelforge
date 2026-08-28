"""Speech-safe cut points: snap helpers + clip_bounds + caption alignment."""

from __future__ import annotations

from pathlib import Path

from reelforge_core.compose.captions import build_captions
from reelforge_core.compose.clips import clip_bounds
from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start
from reelforge_core.models import (
    REELFORGE_VERSION,
    AnalysisConfig,
    AnalysisReport,
    CaptionStyle,
    ComposeConfig,
    RankedReel,
    ReelScores,
    Scene,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)

WORDS = [(10.0, 10.5), (10.7, 11.4), (12.0, 13.5)]


# ---- snap helpers ----------------------------------------------------------


def test_snap_start_extends_to_word_start_within_nudge():
    assert snap_start(10.2, WORDS) == 10.0


def test_snap_start_drops_partial_word_beyond_nudge():
    # 13.5 - 12.0 word; cutting at 12.9 is 0.9s into it (> 0.6 nudge)
    assert snap_start(12.9, WORDS) == 13.5


def test_snap_start_untouched_outside_words():
    assert snap_start(11.5, WORDS) == 11.5
    assert snap_start(10.0, WORDS) == 10.0  # exact boundary


def test_snap_end_extends_to_word_end_within_nudge():
    assert snap_end(11.0, WORDS) == 11.4


def test_snap_end_drops_partial_word_beyond_nudge():
    assert snap_end(12.2, WORDS) == 12.0  # 1.3s left of the word > nudge


def test_snap_end_untouched_outside_words():
    assert snap_end(11.7, WORDS) == 11.7


# ---- clip_bounds -----------------------------------------------------------


def _scene(i: int, start: float, end: float) -> Scene:
    return Scene(
        index=i,
        start_sec=start,
        end_sec=end,
        start_frame=int(start * 30),
        end_frame=int(end * 30),
        thumbnail_path=f"thumbs/scene_{i:04d}.jpg",
    )


def _analysis(scenes: list[Scene], words: list[TranscriptWord] | None) -> AnalysisReport:
    transcript = None
    if words is not None:
        transcript = Transcript(
            language="en",
            language_probability=1.0,
            duration=scenes[-1].end_sec,
            segments=[
                TranscriptSegment(
                    start=words[0].start, end=words[-1].end, text="t", words=words
                )
            ],
        )
    return AnalysisReport(
        asset_id="a",
        source_path="/x.mp4",
        duration=scenes[-1].end_sec,
        width=1920,
        height=1080,
        fps=30,
        has_audio=True,
        config=AnalysisConfig(),
        scenes=scenes,
        transcript=transcript,
        loudness=[],
        semantics=[],
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=0.0,
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage={},
    )


def _reel(scene_indices: list[int], start: float, end: float) -> RankedReel:
    return RankedReel(
        candidate_id="c1",
        scene_indices=scene_indices,
        start_sec=start,
        end_sec=end,
        duration_sec=end - start,
        title="T",
        hook="H",
        justification="J",
        scores=ReelScores(
            narrative_coherence=70,
            hook_strength=70,
            emotional_payoff=70,
            standalone_clarity=70,
        ),
        overall=70.0,
        rank=1,
        suggested_mood="neutral",
    )


def test_clip_bounds_snaps_outer_start_off_word():
    # Scene starts at 10.2 — inside word (10.0, 10.5). Extend back to 10.0.
    scenes = [_scene(0, 10.2, 30.0)]
    words = [TranscriptWord(start=10.0, end=10.5, word=" hey", probability=0.9)]
    analysis = _analysis(scenes, words)
    in_ts, out_ts = clip_bounds(0, 1, scenes[0], ComposeConfig(), analysis)
    assert in_ts == 10.0
    assert out_ts == 30.0


def test_clip_bounds_snaps_outer_end_off_word():
    scenes = [_scene(0, 0.0, 11.0)]
    words = [TranscriptWord(start=10.7, end=11.4, word=" word", probability=0.9)]
    analysis = _analysis(scenes, words)
    _, out_ts = clip_bounds(0, 1, scenes[0], ComposeConfig(), analysis)
    assert out_ts == 11.4


def test_clip_bounds_disabled_flag_keeps_raw_bounds():
    scenes = [_scene(0, 10.2, 30.0)]
    words = [TranscriptWord(start=10.0, end=10.5, word=" hey", probability=0.9)]
    analysis = _analysis(scenes, words)
    cfg = ComposeConfig(speech_safe_cuts=False)
    in_ts, _ = clip_bounds(0, 1, scenes[0], cfg, analysis)
    assert in_ts == 10.2


def test_clip_bounds_no_transcript_keeps_raw_bounds():
    scenes = [_scene(0, 10.2, 30.0)]
    analysis = _analysis(scenes, None)
    in_ts, _ = clip_bounds(0, 1, scenes[0], ComposeConfig(), analysis)
    assert in_ts == 10.2


def test_clip_bounds_snap_applies_after_trim():
    # Trim start +1.0 moves 10.0 -> 11.0, which is inside word (10.7, 11.4)
    # 0.3s in -> extend back to 10.7.
    scenes = [_scene(0, 10.0, 30.0)]
    words = [TranscriptWord(start=10.7, end=11.4, word=" word", probability=0.9)]
    analysis = _analysis(scenes, words)
    cfg = ComposeConfig(trim_start_offset_sec=1.0)
    in_ts, _ = clip_bounds(0, 1, scenes[0], cfg, analysis)
    assert in_ts == 10.7


def test_interior_boundaries_never_snapped():
    scenes = [_scene(0, 0.0, 10.2), _scene(1, 10.2, 20.0), _scene(2, 20.0, 30.0)]
    words = [TranscriptWord(start=10.0, end=10.5, word=" hey", probability=0.9)]
    analysis = _analysis(scenes, words)
    in_ts, out_ts = clip_bounds(1, 3, scenes[1], ComposeConfig(), analysis)
    assert (in_ts, out_ts) == (10.2, 20.0)


# ---- reel-bound clamping (Selection v2) ------------------------------------


def test_clip_bounds_scene_aligned_reel_bounds_are_noop():
    # v2 safety guard: when reel bounds equal the scene edges (every scene-
    # aligned candidate), passing them must change nothing.
    scenes = [_scene(0, 10.0, 40.0)]
    analysis = _analysis(scenes, None)
    cfg = ComposeConfig()
    base = clip_bounds(0, 1, scenes[0], cfg, analysis)
    clamped = clip_bounds(
        0, 1, scenes[0], cfg, analysis, reel_start=10.0, reel_end=40.0
    )
    assert base == clamped == (10.0, 40.0)


def test_clip_bounds_reel_start_clamps_first_clip_mid_scene():
    scenes = [_scene(0, 10.0, 40.0), _scene(1, 40.0, 60.0)]
    analysis = _analysis(scenes, None)
    in_ts, out_ts = clip_bounds(
        0, 2, scenes[0], ComposeConfig(), analysis, reel_start=17.5, reel_end=55.0
    )
    assert (in_ts, out_ts) == (17.5, 40.0)


def test_clip_bounds_reel_end_clamps_last_clip_mid_scene():
    scenes = [_scene(0, 10.0, 40.0), _scene(1, 40.0, 60.0)]
    analysis = _analysis(scenes, None)
    in_ts, out_ts = clip_bounds(
        1, 2, scenes[1], ComposeConfig(), analysis, reel_start=17.5, reel_end=55.0
    )
    assert (in_ts, out_ts) == (40.0, 55.0)


def test_clip_bounds_reel_clamp_applies_before_trim_and_snap():
    # Clamp to 17.0 lands inside word (16.8, 17.4) -> speech snap extends
    # back to the word start, proving clamp runs before the snap step.
    scenes = [_scene(0, 10.0, 40.0)]
    words = [TranscriptWord(start=16.8, end=17.4, word=" hey", probability=0.9)]
    analysis = _analysis(scenes, words)
    in_ts, _ = clip_bounds(
        0, 1, scenes[0], ComposeConfig(), analysis, reel_start=17.0, reel_end=40.0
    )
    assert in_ts == 16.8


# ---- caption alignment -----------------------------------------------------


def test_captions_shift_matches_head_extension(tmp_path: Path):
    """Reel's first scene starts mid-word; the clip extends 0.2s earlier, so
    the word's caption must start at mezzanine t=0 (not clamp weirdly)."""
    scenes = [_scene(0, 10.2, 30.0)]
    words = [
        TranscriptWord(start=10.0, end=10.5, word=" hey", probability=0.9),
        TranscriptWord(start=11.0, end=11.5, word=" there", probability=0.9),
    ]
    analysis = _analysis(scenes, words)
    reel = _reel([0], 10.2, 30.0)
    cfg = ComposeConfig(captions=CaptionStyle(mode="static"))
    out = build_captions(reel, analysis, cfg, tmp_path)
    dialogues = [
        line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
    ]
    assert dialogues, "expected caption events"
    # First event starts at 0:00:00.00 (the extended head) and the second
    # word (source 11.0) lands at 11.0 - 10.0 = 1.0s on the mezzanine.
    start_time = dialogues[0].split(",")[1]
    assert start_time == "0:00:00.00"
    end_time = dialogues[0].split(",")[2]
    assert end_time == "0:00:01.50"


def test_captions_drop_words_trimmed_out(tmp_path: Path):
    """With a start trim past the first word, that word's caption must not
    appear at mezzanine t=0."""
    scenes = [_scene(0, 10.0, 30.0)]
    words = [
        TranscriptWord(start=10.1, end=10.4, word=" gone", probability=0.9),
        TranscriptWord(start=15.0, end=15.5, word=" kept", probability=0.9),
    ]
    analysis = _analysis(scenes, words)
    reel = _reel([0], 10.0, 30.0)
    cfg = ComposeConfig(
        captions=CaptionStyle(mode="static"),
        trim_start_offset_sec=2.0,
        speech_safe_cuts=False,
    )
    out = build_captions(reel, analysis, cfg, tmp_path)
    text = out.read_text()
    assert "gone" not in text
    assert "kept" in text


def test_flatten_words_none_is_empty():
    assert flatten_words(None) == []
