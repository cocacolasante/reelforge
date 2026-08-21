"""Per-shot volume/mute + voiceover mix bus."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph_builder import build_final_command
from reelforge_core.ingest import MediaAsset, ProbeResult
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    ReelTimeline,
    TimelineShot,
    VoiceoverTake,
)
from tests.compose.test_speech_snap import _analysis, _scene


def _clip(i: int, volume: float = 1.0) -> ClipInfo:
    return ClipInfo(
        path=Path(f"/tmp/c{i}.mp4"), scene_index=-1, in_ts=i * 10.0, out_ts=i * 10.0 + 10,
        duration=10.0, has_audio=True, effects_applied=[], asset_id="a", volume=volume,
    )


def _plan(clips, voiceovers=None, **cfg):
    return build_final_command(
        clips=clips,
        analysis=_analysis([_scene(0, 0, 30)], None),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(captions=CaptionStyle(mode="off"), **cfg),
        output_path=Path("/tmp/out.mp4"),
        voiceovers=voiceovers,
    )


# ---- models ----------------------------------------------------------------


def test_shot_effective_gain():
    assert TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5).effective_gain == 1.0
    assert TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5, volume=1.8).effective_gain == 1.8
    assert TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5, volume=9).effective_gain == 3.0
    assert TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5, muted=True, volume=2).effective_gain == 0.0


def test_timeline_round_trips_voiceovers():
    tl = ReelTimeline(
        shots=[TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5)],
        voiceovers=[VoiceoverTake(id="t1", asset_id="vo", start_sec=1.5, duration_sec=3.0, label="intro")],
    )
    again = ReelTimeline(**tl.model_dump())
    assert again.voiceovers[0].label == "intro" and again.voiceovers[0].start_sec == 1.5


def test_audio_only_probe_is_audio():
    pr = ProbeResult(duration_s=4.0, width=0, height=0, fps=0.0, video_codec="none",
                     audio_codec="opus", bit_rate=None, container="matroska,webm")
    a = MediaAsset(id="x", path=Path("/x.webm"), size_bytes=1, probe=pr)
    assert a.is_audio and not a.is_photo and a.has_audio


# ---- per-shot gain in the render graph ------------------------------------


def test_per_clip_volume_applied_before_crossfade():
    plan = _plan([_clip(0), _clip(1, volume=0.5), _clip(2, volume=0.0)])
    fc = plan.filter_complex
    assert "volume=0.5000,asetpts" in fc
    assert "volume=0.0000,asetpts" in fc
    # Unity-gain clips carry no volume filter at all.
    assert fc.count("volume=") == 2


# ---- voiceover bus ---------------------------------------------------------


def test_voiceover_inputs_delayed_gained_and_ducked():
    plan = _plan(
        [_clip(0), _clip(1)],
        voiceovers=[(Path("/tmp/take1.webm"), 2.5, 1.0), (Path("/tmp/take2.webm"), 12.0, 0.8)],
    )
    args = plan.args
    # Two extra inputs after the clips (no music): indices 2 and 3.
    assert args.count("-i") == 4
    assert args[args.index("/tmp/take1.webm") - 1] == "-i"
    fc = plan.filter_complex
    assert "[2:a]" in fc and "[3:a]" in fc
    assert "adelay=delays=2500|2500:all=1" in fc
    assert "adelay=delays=12000|12000:all=1" in fc
    assert "volume=0.8000" in fc
    # Takes are summed, levelled, then footage ducks beneath them.
    assert "[vo_mix]" in fc and "[vo_norm]" in fc
    assert "sidechaincompress" in fc and "[aclips_ducked]" in fc
    # The combined bus feeds the voice loudnorm (so music ducks under both).
    assert "[voice_pre]loudnorm" in fc


def test_voiceover_single_take_no_amix_between_takes():
    plan = _plan([_clip(0)], voiceovers=[(Path("/tmp/t.webm"), 0.0, 1.0)])
    fc = plan.filter_complex
    assert "[vo0]anull[vo_mix]" in fc


def test_voiceover_ducking_can_be_disabled():
    plan = _plan([_clip(0)], voiceovers=[(Path("/tmp/t.webm"), 0.0, 1.0)], voiceover_ducking=False)
    fc = plan.filter_complex
    assert "[aclips_ducked]" not in fc
    assert "[vo_ready]" in fc and "[voice_pre]" in fc


def test_muted_voiceovers_are_skipped():
    plan = _plan([_clip(0)], voiceovers=[(Path("/tmp/t.webm"), 0.0, 0.0)])
    assert plan.args.count("-i") == 1
    assert "[vo_mix]" not in plan.filter_complex


def test_no_voiceovers_graph_unchanged():
    with_none = _plan([_clip(0), _clip(1)], voiceovers=None).filter_complex
    with_empty = _plan([_clip(0), _clip(1)], voiceovers=[]).filter_complex
    assert with_none == with_empty
    assert "[vo_mix]" not in with_none



def test_voiceover_bus_is_padded_to_program_length():
    """A short take must not truncate the mix: the VO bus is padded to the
    full mezzanine duration so sidechain/amix never run out of input."""
    plan = _plan([_clip(0), _clip(1)], voiceovers=[(Path("/tmp/t.webm"), 0.0, 1.0)])
    fc = plan.filter_complex
    # 10 + 10 - 0.4 crossfade = 19.6s
    assert "apad=whole_dur=19.600" in fc
    assert plan.mezzanine_duration_sec == pytest.approx(19.6)
    # Padding happens AFTER levelling so loudnorm doesn't measure silence.
    assert fc.index("loudnorm=I=-12.0") < fc.index("apad=whole_dur=")
