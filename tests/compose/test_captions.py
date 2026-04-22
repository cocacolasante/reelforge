"""Unit tests for the ASS caption builder (no subprocess)."""

from __future__ import annotations

from pathlib import Path

from reelforge_core.compose.captions import (
    _source_to_mezz_time,
    build_captions,
    words_in_reel,
)
from reelforge_core.models import (
    AnalysisReport,
    AnalysisConfig,
    CaptionStyle,
    ComposeConfig,
    RankedReel,
    ReelScores,
    Scene,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    REELFORGE_VERSION,
)


def _scene(i: int, start: float, end: float) -> Scene:
    return Scene(
        index=i,
        start_sec=start,
        end_sec=end,
        start_frame=int(start * 30),
        end_frame=int(end * 30),
        thumbnail_path=f"thumbs/scene_{i:04d}.jpg",
    )


def _reel(scene_indices: list[int], start: float, end: float) -> RankedReel:
    return RankedReel(
        candidate_id="abc123",
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


def _analysis_with_transcript(words: list[TranscriptWord], scenes: list[Scene]) -> AnalysisReport:
    seg = TranscriptSegment(
        start=scenes[0].start_sec if scenes else 0.0,
        end=scenes[-1].end_sec if scenes else 0.0,
        text="placeholder",
        words=words,
    )
    return AnalysisReport(
        asset_id="a",
        source_path="/x.mp4",
        duration=scenes[-1].end_sec if scenes else 0.0,
        width=1920,
        height=1080,
        fps=30,
        has_audio=True,
        config=AnalysisConfig(),
        scenes=scenes,
        transcript=Transcript(
            language="en",
            language_probability=0.99,
            duration=scenes[-1].end_sec if scenes else 0.0,
            segments=[seg],
        ),
        loudness=[],
        semantics=[],
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=0.0,
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage={},
    )


def test_slice_words_to_reel_span() -> None:
    scenes = [_scene(0, 0, 60), _scene(1, 60, 120)]
    words = [
        TranscriptWord(start=5, end=5.5, word=" hi", probability=0.99),
        TranscriptWord(start=35, end=35.5, word=" in", probability=0.99),
        TranscriptWord(start=90, end=90.5, word=" out", probability=0.99),
    ]
    analysis = _analysis_with_transcript(words, scenes)
    reel = _reel([0], 30.0, 60.0)
    selected = words_in_reel(analysis, reel)
    assert [w.word for w in selected] == [" in"]


def test_mezzanine_time_subtracts_xfade_overlap() -> None:
    # 3 scenes of [0-10], [10-20], [20-30]. Reel covers all three. xfade=0.4.
    scenes = [_scene(0, 0, 10), _scene(1, 10, 20), _scene(2, 20, 30)]
    analysis = _analysis_with_transcript([], scenes)
    reel = _reel([0, 1, 2], 0.0, 30.0)

    # A word at source time 12s falls into scene 1 at position 1 in the reel.
    # Mezz time = offset_into_reel(12) - 1 * 0.4 = 12 - 0.4 = 11.6
    t = _source_to_mezz_time(12.0, reel, analysis, 0.4)
    assert abs(t - 11.6) < 1e-6

    # A word at 22s falls into scene 2 at position 2. Mezz = 22 - 2*0.4 = 21.2
    t2 = _source_to_mezz_time(22.0, reel, analysis, 0.4)
    assert abs(t2 - 21.2) < 1e-6


def test_build_captions_off_writes_empty_ass() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        scenes = [_scene(0, 0, 30)]
        analysis = _analysis_with_transcript(
            [TranscriptWord(start=1, end=2, word=" hi", probability=0.99)], scenes
        )
        reel = _reel([0], 0, 30)
        cfg = ComposeConfig(captions=CaptionStyle(mode="off"))
        out = build_captions(reel, analysis, cfg, rd)
        text = out.read_text()
        assert "[Events]" in text
        # No Dialogue lines because mode=off
        assert "Dialogue:" not in text


def test_build_captions_escapes_braces() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        scenes = [_scene(0, 0, 30)]
        analysis = _analysis_with_transcript(
            [TranscriptWord(start=1, end=2, word="{weird}", probability=0.99)], scenes
        )
        reel = _reel([0], 0, 30)
        cfg = ComposeConfig(
            captions=CaptionStyle(mode="static", max_chars_per_line=40)
        )
        out = build_captions(reel, analysis, cfg, rd)
        text = out.read_text()
        assert r"\{weird\}" in text


def test_build_captions_karaoke_one_event_per_word() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        scenes = [_scene(0, 0, 30)]
        analysis = _analysis_with_transcript(
            [
                TranscriptWord(start=1, end=1.5, word=" hi", probability=0.9),
                TranscriptWord(start=1.6, end=2.2, word=" there", probability=0.9),
                TranscriptWord(start=2.3, end=3.0, word=" friend", probability=0.9),
            ],
            scenes,
        )
        reel = _reel([0], 0, 30)
        cfg = ComposeConfig(captions=CaptionStyle(mode="karaoke"))
        out = build_captions(reel, analysis, cfg, rd)
        dialogue_lines = [
            line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogue_lines) == 3
