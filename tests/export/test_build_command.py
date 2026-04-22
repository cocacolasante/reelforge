from __future__ import annotations

from pathlib import Path

from reelforge_core.export import PRESETS
from reelforge_core.export.command import build_export_command


def _cmd_for(preset_id: str) -> list[str]:
    return build_export_command(
        Path("/data/mezz.mp4"),
        Path(f"/data/out/{preset_id}.{PRESETS[preset_id].container}"),
        PRESETS[preset_id],
    )


def test_h264_social_command() -> None:
    cmd = _cmd_for("mp4_h264_social")
    joined = " ".join(cmd)
    # Pure transcode — no filters
    assert "-vf" not in cmd
    assert "-af" not in cmd
    assert "-filter_complex" not in cmd
    # Codec
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "aac"
    # Pixel format
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    # Stream map
    assert "-map" in cmd
    map_indices = [i for i, a in enumerate(cmd) if a == "-map"]
    assert [cmd[i + 1] for i in map_indices] == ["0:v:0", "0:a:0"]
    # Preset + CRF + profile + level
    assert "-crf" in cmd and cmd[cmd.index("-crf") + 1] == "20"
    assert "-preset" in cmd and cmd[cmd.index("-preset") + 1] == "medium"
    assert "-profile:v" in cmd and cmd[cmd.index("-profile:v") + 1] == "high"
    assert "-level" in cmd and cmd[cmd.index("-level") + 1] == "4.1"
    # Audio bitrate
    assert "-b:a" in cmd and cmd[cmd.index("-b:a") + 1] == "192k"
    # Faststart
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    # Deterministic metadata
    assert "creation_time=1970-01-01T00:00:00Z" in joined
    assert "encoder=reelforge" in joined


def test_h265_hq_command_has_hvc1_and_libx265() -> None:
    cmd = _cmd_for("mp4_h265_hq")
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
    assert cmd[cmd.index("-crf") + 1] == "22"
    assert cmd[cmd.index("-x265-params") + 1] == "log-level=error"


def test_prores_422_command_has_profile_2_apl0_422p10() -> None:
    cmd = _cmd_for("mov_prores_422")
    assert cmd[cmd.index("-c:v") + 1] == "prores_ks"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv422p10le"
    assert cmd[cmd.index("-profile:v") + 1] == "2"
    assert cmd[cmd.index("-vendor") + 1] == "apl0"
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
    # No bitrate arg for PCM
    assert "-b:a" not in cmd


def test_prores_hq_command_has_profile_3() -> None:
    cmd = _cmd_for("mov_prores_hq")
    assert cmd[cmd.index("-profile:v") + 1] == "3"


def test_command_output_path_is_last_arg() -> None:
    cmd = _cmd_for("mp4_h264_social")
    assert cmd[-1].endswith("mp4_h264_social.mp4")


def test_command_never_shells_out_unsafely() -> None:
    # No shell metacharacters in the canonical commands.
    cmd = _cmd_for("mp4_h264_social")
    for a in cmd:
        assert ";" not in a
        assert "&&" not in a
