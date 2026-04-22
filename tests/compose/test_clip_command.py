from __future__ import annotations

from pathlib import Path

from reelforge_core.compose.clips import build_clip_command
from reelforge_core.models import ComposeConfig


def test_clip_command_9x16_with_audio() -> None:
    cmd = build_clip_command(
        source=Path("/data/inbox/x.mp4"),
        out_path=Path("/data/working/aid/reels/rid/clips/clip_0000.mp4"),
        in_ts=10.25,
        out_ts=15.0,
        config=ComposeConfig(aspect="9:16", target_fps=30),
        has_audio=True,
        is_hdr=False,
    )
    # Accurate seek (-ss after -i) and exact out
    assert "-ss" in cmd and "10.250" in cmd
    assert "-to" in cmd and "15.000" in cmd
    # scale+pad to 1080x1920
    vf_idx = cmd.index("-vf") + 1
    assert "scale=1080:1920" in cmd[vf_idx]
    assert "pad=1080:1920" in cmd[vf_idx]
    assert "fps=30" in cmd[vf_idx]
    # audio normalization + AAC
    assert "-af" in cmd
    assert "-c:a" in cmd and "aac" in cmd
    # bitexact flags for deterministic intermediates
    assert "+bitexact" in " ".join(cmd)


def test_clip_command_silent_source_omits_audio() -> None:
    cmd = build_clip_command(
        source=Path("/x.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=0,
        out_ts=5,
        config=ComposeConfig(),
        has_audio=False,
        is_hdr=False,
    )
    assert "-an" in cmd
    assert "-af" not in cmd
    assert "aac" not in cmd


def test_clip_command_hdr_inserts_tonemap() -> None:
    cmd = build_clip_command(
        source=Path("/x.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=0,
        out_ts=5,
        config=ComposeConfig(),
        has_audio=True,
        is_hdr=True,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "zscale" in vf
    assert "tonemap" in vf
    # tonemap must precede the actual `scale=WxH` (not `zscale=`)
    assert vf.index("tonemap") < vf.index("scale=1080")


def test_clip_command_aspect_16x9_target_resolution() -> None:
    cmd = build_clip_command(
        source=Path("/x.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=0,
        out_ts=5,
        config=ComposeConfig(aspect="16:9"),
        has_audio=True,
        is_hdr=False,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf
    assert "pad=1920:1080" in vf


def test_clip_command_aspect_1x1_square() -> None:
    cmd = build_clip_command(
        source=Path("/x.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=0,
        out_ts=5,
        config=ComposeConfig(aspect="1:1"),
        has_audio=True,
        is_hdr=False,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1080:1080" in vf
