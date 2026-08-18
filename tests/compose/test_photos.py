"""Photo shots: command shape, interleaving, and caption timing with photos."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.photos import (
    build_photo_clip_command,
    interleave_photo_clips,
)
from reelforge_core.models import ComposeConfig, PhotoInsert


def _clip(idx: int, dur: float = 10.0, photo: bool = False) -> ClipInfo:
    return ClipInfo(
        path=Path(f"/tmp/c{idx}.mp4"),
        scene_index=-1 if photo else idx,
        in_ts=0.0 if photo else idx * 10.0,
        out_ts=dur if photo else idx * 10.0 + dur,
        duration=dur,
        has_audio=True,
        effects_applied=[],
        is_photo=photo,
    )


# ---- command shape ---------------------------------------------------------


def test_photo_command_loops_still_and_adds_silent_audio():
    cmd = build_photo_clip_command(
        source=Path("/p.jpg"),
        out_path=Path("/o.mp4"),
        duration_sec=3.0,
        config=ComposeConfig(aspect="9:16"),
    )
    assert "-loop" in cmd and cmd[cmd.index("-loop") + 1] == "1"
    # Duration is bounded on the still input AND the silent bed.
    assert cmd.count("-t") == 2
    assert "anullsrc=r=48000:cl=stereo" in cmd
    assert "-shortest" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    # Ken Burns: cover the frame, then drift a target-sized window.
    assert "force_original_aspect_ratio=increase" in vf
    assert "crop=1080:1920" in vf
    assert "fps=30" in vf


def test_photo_command_without_ken_burns_letterboxes():
    cmd = build_photo_clip_command(
        source=Path("/p.jpg"),
        out_path=Path("/o.mp4"),
        duration_sec=2.0,
        config=ComposeConfig(aspect="16:9"),
        ken_burns=False,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "pad=1920:1080" in vf
    assert "crop=" not in vf


def test_photo_pan_direction_rotates():
    seen = set()
    for i in range(4):
        cmd = build_photo_clip_command(
            source=Path("/p.jpg"),
            out_path=Path("/o.mp4"),
            duration_sec=2.0,
            config=ComposeConfig(),
            pan_index=i,
        )
        seen.add(cmd[cmd.index("-vf") + 1])
    assert len(seen) == 4, "consecutive photos should not all drift the same way"


# ---- interleaving ----------------------------------------------------------


def test_interleave_intro_and_outro():
    videos = [_clip(0), _clip(1)]
    photos = [(0, _clip(90, 3.0, photo=True)), (2, _clip(91, 3.0, photo=True))]
    out = interleave_photo_clips(videos, photos)
    assert [c.is_photo for c in out] == [True, False, False, True]


def test_interleave_between_clips():
    videos = [_clip(0), _clip(1), _clip(2)]
    out = interleave_photo_clips(videos, [(2, _clip(90, 3.0, photo=True))])
    assert [c.is_photo for c in out] == [False, False, True, False]


def test_interleave_clamps_out_of_range_positions():
    videos = [_clip(0)]
    out = interleave_photo_clips(
        videos, [(-5, _clip(90, 1.0, photo=True)), (99, _clip(91, 1.0, photo=True))]
    )
    assert [c.is_photo for c in out] == [True, False, True]


def test_interleave_preserves_order_at_same_position():
    videos = [_clip(0)]
    a = _clip(90, 1.0, photo=True)
    b = _clip(91, 2.0, photo=True)
    out = interleave_photo_clips(videos, [(0, a), (0, b)])
    assert out[0] is a and out[1] is b


def test_interleave_no_photos_is_identity():
    videos = [_clip(0), _clip(1)]
    assert interleave_photo_clips(videos, []) == videos


# ---- caption timing with photos -------------------------------------------


def test_captions_shift_past_inserted_photo(tmp_path: Path):
    """A 3s photo before the footage pushes every caption 3s later."""
    from tests.compose.test_speech_snap import _analysis, _reel, _scene
    from reelforge_core.compose.captions import build_captions
    from reelforge_core.models import CaptionStyle, TranscriptWord

    scenes = [_scene(0, 0.0, 30.0)]
    words = [TranscriptWord(start=5.0, end=5.5, word=" hey", probability=0.9)]
    analysis = _analysis(scenes, words)
    reel = _reel([0], 0.0, 30.0)
    cfg = ComposeConfig(captions=CaptionStyle(mode="static"), speech_safe_cuts=False)

    video = _clip(0, 30.0)
    video = ClipInfo(
        path=video.path, scene_index=0, in_ts=0.0, out_ts=30.0, duration=30.0,
        has_audio=True, effects_applied=[],
    )
    photo = _clip(90, 3.0, photo=True)

    # Without the photo: word at source 5s -> mezzanine 5s.
    plain = build_captions(reel, analysis, cfg, tmp_path, clips=[video])
    assert plain.read_text().splitlines()[-1].split(",")[1] == "0:00:05.00"

    # With a 3s photo first (xfade 0.4 reclaims 0.4s): 5 + 3 - 0.4 = 7.6s.
    with_photo = build_captions(
        reel, analysis, cfg, tmp_path, clips=[photo, video]
    )
    assert with_photo.read_text().splitlines()[-1].split(",")[1] == "0:00:07.60"


# ---- real render (integration) --------------------------------------------


def test_photo_clip_renders_playable_shot(tmp_path: Path):
    """Run FFmpeg for real: a still becomes a normalized, playable shot."""
    import json
    import subprocess

    photo = tmp_path / "p.jpg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=1600x1200", "-frames:v", "1", "-y", str(photo)],
        check=True,
    )
    out = tmp_path / "shot.mp4"
    cmd = build_photo_clip_command(
        source=photo,
        out_path=out,
        duration_sec=2.0,
        config=ComposeConfig(aspect="9:16"),
    )
    subprocess.run(cmd, check=True, capture_output=True)
    assert out.exists() and out.stat().st_size > 1000

    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(out)],
            check=True, capture_output=True, text=True,
        ).stdout
    )
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["pix_fmt"] == "yuv420p"
    # Silent bed present so the xfade/acrossfade chain has an audio stream.
    assert audio["codec_name"] == "aac"
    assert int(audio["sample_rate"]) == 48000
    assert abs(float(probe["format"]["duration"]) - 2.0) < 0.15
