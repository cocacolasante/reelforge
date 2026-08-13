from __future__ import annotations

from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    LoudnessPoint,
    Scene,
    SceneSemantics,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    UsageTotals,
    compute_overall,
)


def test_scene_roundtrip() -> None:
    s = Scene(
        index=0,
        start_sec=0.0,
        end_sec=3.5,
        start_frame=0,
        end_frame=105,
        thumbnail_path="thumbs/scene_0000.jpg",
    )
    assert Scene.model_validate(s.model_dump()) == s


def test_transcript_roundtrip() -> None:
    t = Transcript(
        language="en",
        language_probability=0.99,
        duration=3.0,
        segments=[
            TranscriptSegment(
                start=0.0,
                end=1.0,
                text=" hi",
                words=[TranscriptWord(start=0.1, end=0.3, word=" hi", probability=0.95)],
            )
        ],
    )
    assert Transcript.model_validate_json(t.model_dump_json()) == t


def test_loudness_roundtrip() -> None:
    p = LoudnessPoint(time_sec=0.5, lufs=-23.0)
    assert LoudnessPoint.model_validate(p.model_dump()) == p


def test_semantics_roundtrip_and_defaults() -> None:
    s = SceneSemantics(
        scene_index=2,
        summary="A quick test.",
        tags=["a", "b", "c"],
        mood="calm",
        has_speech=True,
        visual_energy="low",
    )
    assert s.cached is False
    assert SceneSemantics.model_validate(s.model_dump()) == s


def test_analysis_config_defaults() -> None:
    c = AnalysisConfig()
    assert c.whisper_model == "base.en"
    assert c.semantics_concurrency == 5
    assert c.semantics_prompt_version == "v1"


def test_compute_overall_monotonic() -> None:
    # Sanity: moving through stages never decreases overall.
    seq = [
        compute_overall("probe", 0.0),
        compute_overall("probe", 1.0),
        compute_overall("scenes", 0.0),
        compute_overall("scenes", 1.0),
        compute_overall("transcribe", 1.0),
        compute_overall("loudness", 1.0),
        compute_overall("semantics", 1.0),
    ]
    for a, b in zip(seq, seq[1:]):
        assert b >= a
    assert abs(seq[-1] - 1.0) < 1e-9


def test_usage_totals_accumulates() -> None:
    u = UsageTotals(input_tokens=10, output_tokens=5, cache_hits=2)
    assert u.input_tokens == 10


def test_analysis_report_json_roundtrip() -> None:
    r = AnalysisReport(
        asset_id="abc",
        source_path="/data/inbox/x.mp4",
        duration=10.0,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=True,
        config=AnalysisConfig(),
        scenes=[],
        transcript=None,
        loudness=[],
        semantics=[],
        created_at="2026-04-22T00:00:00+00:00",
        elapsed_sec=1.5,
        reelforge_version="0.2.0",
        anthropic_usage={"input_tokens": 0, "output_tokens": 0, "cache_hits": 0},
    )
    assert AnalysisReport.model_validate_json(r.model_dump_json()) == r


def test_quality_presets_map_both_encode_stages():
    from reelforge_core.models import ComposeConfig

    high = ComposeConfig(quality="high")
    assert (high.effective_mezz_preset, high.effective_mezz_crf) == ("slow", 16)
    assert (high.clip_preset, high.clip_crf) == ("fast", 16)
    draft = ComposeConfig(quality="draft")
    assert (draft.effective_mezz_preset, draft.effective_mezz_crf) == ("veryfast", 20)
    std = ComposeConfig()
    assert (std.effective_mezz_preset, std.effective_mezz_crf) == ("medium", 18)
    assert (std.clip_preset, std.clip_crf) == ("ultrafast", 18)


def test_quality_explicit_crf_and_preset_override():
    from reelforge_core.models import ComposeConfig

    cfg = ComposeConfig(quality="high", video_crf=22, video_preset="veryfast")
    assert cfg.effective_mezz_crf == 22
    assert cfg.effective_mezz_preset == "veryfast"
