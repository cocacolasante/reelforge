"""Unit tests for build_final_command — shape/arg assertions, no FFmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph_builder import _xfade_offsets, build_final_command
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    Scene,
    SceneSemantics,
    TransitionStyle,
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


def _analysis(n: int, energy: str = "medium") -> AnalysisReport:
    scenes = [_scene(i, i * 10.0, (i + 1) * 10.0) for i in range(n)]
    sems = [
        SceneSemantics(
            scene_index=i,
            summary="s",
            tags=["a", "b", "c"],
            mood="neutral",
            has_speech=True,
            visual_energy=energy,  # type: ignore[arg-type]
        )
        for i in range(n)
    ]
    return AnalysisReport(
        asset_id="a",
        source_path="/x.mp4",
        duration=n * 10.0,
        width=1920,
        height=1080,
        fps=30,
        has_audio=True,
        config=AnalysisConfig(),
        scenes=scenes,
        transcript=None,
        loudness=[],
        semantics=sems,
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=0.0,
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage={},
    )


def _clip(idx: int, duration: float, has_audio: bool = True) -> ClipInfo:
    return ClipInfo(
        path=Path(f"/tmp/clip_{idx:04d}.mp4"),
        scene_index=idx,
        in_ts=idx * 10.0,
        out_ts=(idx + 1) * 10.0,
        duration=duration,
        has_audio=has_audio,
        effects_applied=[],
    )


def test_xfade_offsets_three_clips() -> None:
    # Clips [5, 6, 4] with xfade 0.4 → offsets [5-0.4, 5+6-0.8] = [4.6, 10.2]
    offsets = _xfade_offsets([5.0, 6.0, 4.0], 0.4)
    assert offsets == pytest.approx([4.6, 10.2])


def test_xfade_offsets_single_clip_has_none() -> None:
    assert _xfade_offsets([5.0], 0.4) == []


def test_build_command_three_clips_fade_with_music_and_captions() -> None:
    clips = [_clip(0, 15.0), _clip(1, 15.0), _clip(2, 15.0)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis(3),
        music_path=Path("/tmp/music.wav"),
        captions_path=Path("/tmp/captions.ass"),
        config=ComposeConfig(effects=EffectsConfig(unsharp=False)),
        output_path=Path("/tmp/mezz.mp4"),
    )
    # Three clip inputs + one music input
    assert plan.args.count("-i") == 4
    assert plan.music_input_index == 3
    fc = plan.filter_complex
    # Two xfade transitions for 3 clips
    assert fc.count("xfade=") == 2
    assert fc.count("acrossfade=") == 2
    # subtitles filter present
    assert "subtitles=" in fc
    # sidechaincompress mix
    assert "sidechaincompress=" in fc
    assert "amix=" in fc
    # Expected mezzanine duration: 45 - 2*0.4 = 44.2
    assert plan.mezzanine_duration_sec == pytest.approx(44.2)


def test_build_command_no_music_skips_music_filters() -> None:
    clips = [_clip(0, 15.0), _clip(1, 15.0)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis(2),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(captions=CaptionStyle(mode="off"), effects=EffectsConfig(unsharp=False)),
        output_path=Path("/tmp/out.mp4"),
    )
    assert "sidechaincompress=" not in plan.filter_complex
    assert "amix=" not in plan.filter_complex
    # voice chain ends at anull → afinal
    assert "[afinal]" in plan.filter_complex


def test_build_command_no_captions_skips_subtitles_filter() -> None:
    clips = [_clip(0, 15.0)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis(1),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(captions=CaptionStyle(mode="off")),
        output_path=Path("/tmp/out.mp4"),
    )
    assert "subtitles=" not in plan.filter_complex


def test_build_command_low_energy_scenes_get_kenburns() -> None:
    clips = [_clip(0, 10.0), _clip(1, 10.0)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis(2, energy="low"),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(
            captions=CaptionStyle(mode="off"),
            effects=EffectsConfig(ken_burns_on_low_energy=True, unsharp=False),
        ),
        output_path=Path("/tmp/out.mp4"),
    )
    assert "zoompan=" in plan.filter_complex
    # Expect one zoompan per low-energy clip
    assert plan.filter_complex.count("zoompan=") == 2


def test_build_command_includes_deterministic_flags() -> None:
    clips = [_clip(0, 10.0)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis(1),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(
            captions=CaptionStyle(mode="off"), effects=EffectsConfig(unsharp=False)
        ),
        output_path=Path("/tmp/out.mp4"),
    )
    joined = " ".join(plan.args)
    assert "+bitexact" in joined
    assert "creation_time=1970-01-01T00:00:00Z" in joined
