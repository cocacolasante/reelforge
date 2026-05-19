"""Unit tests for compose/montage.py build_montage_command (no FFmpeg run)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _build_tiny_mp4(out: Path, duration: float = 1.0) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=320x240:d={duration}:r=25",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def two_chapters(tmp_path: Path) -> list[Path]:
    a = tmp_path / "chap_a.mp4"
    b = tmp_path / "chap_b.mp4"
    _build_tiny_mp4(a, duration=2.0)
    _build_tiny_mp4(b, duration=3.0)
    return [a, b]


def test_command_includes_xfade_for_each_transition(two_chapters: list[Path]) -> None:
    from reelforge_core.compose.montage import build_montage_command

    cmd, total = build_montage_command(
        inputs=two_chapters,
        output_path=Path("/tmp/out.mp4"),
        transition_duration=0.4,
        target_resolution=(1080, 1920),
        target_fps=30,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("xfade=") == 1  # N-1 transitions for N inputs
    assert fc.count("acrossfade=") == 1
    # 2 + 3 - 0.4 = 4.6
    assert abs(total - 4.6) < 0.01


def test_command_single_input_no_xfade(two_chapters: list[Path]) -> None:
    from reelforge_core.compose.montage import build_montage_command

    cmd, total = build_montage_command(
        inputs=[two_chapters[0]],
        output_path=Path("/tmp/out.mp4"),
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "xfade=" not in fc
    assert abs(total - 2.0) < 0.01


def test_command_empty_inputs_raises() -> None:
    from reelforge_core.compose.montage import build_montage_command
    from reelforge_core.errors import ComposeError

    with pytest.raises(ComposeError):
        build_montage_command(inputs=[], output_path=Path("/tmp/out.mp4"))


def test_command_has_bitexact_flags(two_chapters: list[Path]) -> None:
    from reelforge_core.compose.montage import build_montage_command

    cmd, _ = build_montage_command(inputs=two_chapters, output_path=Path("/tmp/out.mp4"))
    joined = " ".join(cmd)
    assert "+bitexact" in joined
    assert "creation_time=1970-01-01T00:00:00Z" in joined


def test_command_targets_correct_resolution(two_chapters: list[Path]) -> None:
    from reelforge_core.compose.montage import build_montage_command

    cmd, _ = build_montage_command(
        inputs=two_chapters,
        output_path=Path("/tmp/out.mp4"),
        target_resolution=(1920, 1080),
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1920:1080" in fc


def test_offsets_chain_correctly_for_three_inputs(tmp_path: Path) -> None:
    """Confirm the cumulative offset math: clips of [2, 3, 4] with 0.5 xfade
    should produce xfade offsets [1.5, 4.0] (each = sum-prev-clip-durations
    minus per-transition overlap)."""
    paths = []
    for i, d in enumerate([2.0, 3.0, 4.0]):
        p = tmp_path / f"c{i}.mp4"
        _build_tiny_mp4(p, duration=d)
        paths.append(p)

    from reelforge_core.compose.montage import build_montage_command

    cmd, total = build_montage_command(
        inputs=paths, output_path=tmp_path / "out.mp4", transition_duration=0.5
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    # offset between clip 0 and 1 = 2.0 - 0.5 = 1.5
    # offset between merged-A-B and clip 2 = (2.0 - 0.5) + (3.0 - 0.5) = 4.0
    assert "offset=1.500" in fc
    assert "offset=4.000" in fc
    # total = 2 + 3 + 4 - 2*0.5 = 8.0
    assert abs(total - 8.0) < 0.01
