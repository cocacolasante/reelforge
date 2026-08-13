"""Unit tests for the ASS caption builder (no subprocess)."""

from __future__ import annotations

from pathlib import Path

from reelforge_core.compose.captions import (
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
    import tempfile

    # 3 scenes of [0-10], [10-20], [20-30]. Reel covers all three. xfade=0.4.
    # A word at source 12s (scene position 1): mezz = 12 - 1*0.4 = 11.6.
    scenes = [_scene(0, 0, 10), _scene(1, 10, 20), _scene(2, 20, 30)]
    words = [TranscriptWord(start=12.0, end=12.5, word=" hey", probability=0.9)]
    analysis = _analysis_with_transcript(words, scenes)
    reel = _reel([0, 1, 2], 0.0, 30.0)
    with tempfile.TemporaryDirectory() as td:
        out = build_captions(
            reel, analysis, ComposeConfig(captions=CaptionStyle(mode="static")), Path(td)
        )
        dialogues = [
            line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogues) == 1
        assert dialogues[0].split(",")[1] == "0:00:11.60"
        assert dialogues[0].split(",")[2] == "0:00:12.10"


def test_beat_trims_shift_caption_times() -> None:
    import tempfile

    # Same 3-scene reel; beat sync trims 0.5s off clip 0's end. A word in
    # scene 1 shifts 0.5s earlier: 12 - 0.5 - 0.4 = 11.1.
    scenes = [_scene(0, 0, 10), _scene(1, 10, 20), _scene(2, 20, 30)]
    words = [TranscriptWord(start=12.0, end=12.5, word=" hey", probability=0.9)]
    analysis = _analysis_with_transcript(words, scenes)
    reel = _reel([0, 1, 2], 0.0, 30.0)
    with tempfile.TemporaryDirectory() as td:
        out = build_captions(
            reel,
            analysis,
            ComposeConfig(captions=CaptionStyle(mode="static")),
            Path(td),
            end_trims=[0.5, 0.0],
        )
        dialogues = [
            line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogues) == 1
        assert dialogues[0].split(",")[1] == "0:00:11.10"


def test_words_in_trimmed_tail_are_dropped() -> None:
    import tempfile

    # Word at 9.8s sits in the 0.5s tail trimmed off clip 0 -> dropped.
    scenes = [_scene(0, 0, 10), _scene(1, 10, 20)]
    words = [TranscriptWord(start=9.7, end=9.95, word=" gone", probability=0.9)]
    analysis = _analysis_with_transcript(words, scenes)
    reel = _reel([0, 1], 0.0, 20.0)
    with tempfile.TemporaryDirectory() as td:
        out = build_captions(
            reel,
            analysis,
            ComposeConfig(captions=CaptionStyle(mode="static")),
            Path(td),
            end_trims=[0.5],
        )
        assert "gone" not in out.read_text()


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


def test_karaoke_full_line_visible_with_moving_highlight() -> None:
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
        dialogues = [
            line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogues) == 3
        # Every event shows the complete line, not just the active word.
        for d in dialogues:
            for tok in ("hi", "there", "friend"):
                assert tok in d
        # The highlight override moves: event k wraps token k.
        assert "{\\c&H00FFFF&\\b1}hi{\\r} there friend" in dialogues[0]
        assert "hi {\\c&H00FFFF&\\b1}there{\\r} friend" in dialogues[1]
        assert "hi there {\\c&H00FFFF&\\b1}friend{\\r}" in dialogues[2]


def test_karaoke_events_are_contiguous_within_line() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        scenes = [_scene(0, 0, 30)]
        analysis = _analysis_with_transcript(
            [
                TranscriptWord(start=1, end=1.5, word=" hi", probability=0.9),
                # 0.4s pause before "there" — the line must not flicker.
                TranscriptWord(start=1.9, end=2.2, word=" there", probability=0.9),
            ],
            scenes,
        )
        reel = _reel([0], 0, 30)
        cfg = ComposeConfig(captions=CaptionStyle(mode="karaoke"))
        out = build_captions(reel, analysis, cfg, rd)
        dialogues = [
            line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogues) == 2
        end_first = dialogues[0].split(",")[2]
        start_second = dialogues[1].split(",")[1]
        assert end_first == start_second


def test_karaoke_long_text_splits_into_short_lines() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        scenes = [_scene(0, 0, 30)]
        words = [
            TranscriptWord(start=1 + i * 0.5, end=1.3 + i * 0.5, word=f" word{i}", probability=0.9)
            for i in range(8)
        ]
        analysis = _analysis_with_transcript(words, scenes)
        reel = _reel([0], 0, 30)
        cfg = ComposeConfig(captions=CaptionStyle(mode="karaoke", karaoke_max_chars=12))
        out = build_captions(reel, analysis, cfg, rd)
        dialogues = [
            line for line in out.read_text().splitlines() if line.startswith("Dialogue:")
        ]
        # Still one event per word...
        assert len(dialogues) == 8
        # ...but no event carries more than 2 tokens (12 chars fits "word0 word1").
        for d in dialogues:
            text = d.split(",,", 1)[1]
            assert text.count("word") <= 2
