"""Short "cut" crossfades between clips whose video is a frame shorter than
planned. ffmpeg 5.1's xfade dropped the whole next clip when the running
video total fell short of a 0.04s transition's end, sliding the picture
ahead of the sound for the rest of the render."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph_builder import CLIP_END_PAD_SEC, build_final_command
from reelforge_core.models import CaptionStyle, ComposeConfig, EffectsConfig
from tests.compose.test_speech_snap import _analysis, _scene

COLORS = ["red", "green", "blue", "yellow", "magenta"]


def _config(**kw) -> ComposeConfig:
    return ComposeConfig(
        aspect="16:9",
        video_preset="ultrafast",
        no_music=True,
        normalize_loudness=False,
        captions=CaptionStyle(mode="off"),
        effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False, lut=None),
        smart_mode=False,
        **kw,
    )


def _info(path: Path, duration: float) -> ClipInfo:
    return ClipInfo(path=path, scene_index=-1, in_ts=0.0, out_ts=duration, duration=duration,
                    has_audio=True, effects_applied=[])


def test_every_clip_gets_end_slack_and_audio_is_pinned_to_plan():
    clips = [_info(Path(f"/tmp/c{i}.mp4"), 3.58) for i in range(3)]
    plan = build_final_command(clips=clips, analysis=_analysis([_scene(0, 0, 30)], None),
                               music_path=None, captions_path=None, config=_config(),
                               output_path=Path("/tmp/out.mp4"), transitions=[("fade", 0.04)] * 2)
    fc = plan.args[plan.args.index("-filter_complex") + 1]
    assert fc.count(f"tpad=stop_mode=clone:stop_duration={CLIP_END_PAD_SEC:.3f}") == 3
    assert fc.count("trim=duration=3.580,setpts=PTS-STARTPTS") == 1  # only the last clip
    assert fc.count("apad=whole_dur=3.580,atrim=duration=3.580") == 3


def _probe(path: Path) -> dict[str, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(out)["streams"]}


def test_frame_short_clips_keep_every_shot_with_cut_transitions(tmp_path: Path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    fps = 30
    planned = 3.58  # 106 frames = 3.5333s of video: short of plan by more than a 0.04s cut
    clips = []
    for i, color in enumerate(COLORS):
        path = tmp_path / f"c{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r={fps}:d={106 / fps:.4f}",
             "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={planned}",
             "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(path)],
            check=True,
        )
        clips.append(_info(path, planned))
    out = tmp_path / "out.mp4"
    plan = build_final_command(clips=clips, analysis=_analysis([_scene(0, 0, 30)], None),
                               music_path=None, captions_path=None, config=_config(target_fps=fps),
                               output_path=out, transitions=[("fade", 0.04)] * 4)
    subprocess.run(plan.args, check=True, capture_output=True)

    expected = 5 * planned - 4 * 0.04
    streams = _probe(out)
    # Old graph: every cut ran past the earlier clip's last frame and the
    # video came out short of the audio (here 0.2s; on real footage xfade
    # dropped whole clips — 8s over a 5-minute mix).
    assert streams["video"] == pytest.approx(expected, abs=0.1)
    assert streams["audio"] == pytest.approx(expected, abs=0.1)
    # The final shot is on screen at the end.
    frame = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{expected - 0.5:.2f}", "-i", str(out), "-frames:v", "1",
         "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    r, g, b = frame[0], frame[1], frame[2]
    assert r > 150 and b > 150 and g < 100  # magenta
