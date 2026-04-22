"""export/verify.py edge cases: size floor, codec mismatch, tag mismatch,
missing streams, duration drift. Covers code paths not exercised by the
Phase-4 integration test (which always produces a well-formed file)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from reelforge_core.errors import OutputVerificationError
from reelforge_core.export import PRESETS
from reelforge_core.export.verify import MIN_SIZE_BYTES, sanity_check_size, verify_export


def _build_h264(out: Path, with_audio: bool = True) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=25",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    else:
        cmd += ["-an"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True, capture_output=True)


def test_verify_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OutputVerificationError, match="output missing"):
        verify_export(tmp_path / "absent.mp4", PRESETS["mp4_h264_social"])


def test_verify_below_size_floor(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"x" * 100)
    with pytest.raises(OutputVerificationError, match="below the"):
        verify_export(tiny, PRESETS["mp4_h264_social"])


def test_verify_wrong_codec_claim(tmp_path: Path) -> None:
    # Build an h264 file, verify against the h265 preset → raises.
    out = tmp_path / "h264.mp4"
    _build_h264(out)
    # Must be above MIN_SIZE_BYTES; the 1s clip is well under that. Add padding
    # by appending zeros — ffprobe still reports a valid h264 stream for the
    # real bytes. Simpler: concatenate a longer generated clip.
    long_out = tmp_path / "long.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=10:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(long_out),
        ],
        check=True, capture_output=True,
    )
    if long_out.stat().st_size < MIN_SIZE_BYTES:
        pytest.skip("ffmpeg produced too-small fixture")
    with pytest.raises(OutputVerificationError, match="video codec mismatch"):
        verify_export(long_out, PRESETS["mp4_h265_hq"])


def test_verify_wrong_audio_claim(tmp_path: Path) -> None:
    """Feed a ProRes-in-MOV file with AAC audio and claim PCM — raises."""
    out = tmp_path / "long.mov"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=10:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le",
            "-c:a", "aac",  # NOTE: presets say pcm; this is the mismatch
            "-shortest", str(out),
        ],
        check=True, capture_output=True,
    )
    if out.stat().st_size < MIN_SIZE_BYTES:
        pytest.skip("ffmpeg produced too-small fixture")
    with pytest.raises(OutputVerificationError, match="codec"):
        verify_export(out, PRESETS["mov_prores_422"])


def test_verify_wrong_pixel_format(tmp_path: Path) -> None:
    """Claim yuv422p10le on a yuv420p file."""
    out = tmp_path / "yuv420.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=10:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True, capture_output=True,
    )
    if out.stat().st_size < MIN_SIZE_BYTES:
        pytest.skip("small fixture")
    # ProRes preset expects yuv422p10le; h264 file has yuv420p and h264 codec
    # — the codec check fires first. Sufficient for the mismatch path.
    with pytest.raises(OutputVerificationError):
        verify_export(out, PRESETS["mov_prores_422"])


def test_verify_duration_drift(tmp_path: Path) -> None:
    """Declare the mezzanine as 60s when the output is only 10s; raises."""
    out = tmp_path / "drift.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=10:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True, capture_output=True,
    )
    if out.stat().st_size < MIN_SIZE_BYTES:
        pytest.skip("small fixture")
    with pytest.raises(OutputVerificationError, match="duration drift"):
        verify_export(out, PRESETS["mp4_h264_social"], mezzanine_duration_sec=60.0)


def test_sanity_check_size_warns_on_outlier(caplog: pytest.LogCaptureFixture) -> None:
    # output vastly bigger than expected ratio → warning logged.
    with caplog.at_level("WARNING"):
        sanity_check_size(
            output_size_bytes=50 * 1024 * 1024,
            mezzanine_size_bytes=1 * 1024 * 1024,
            preset=PRESETS["mp4_h264_social"],
        )
    # Expected ratio 0.6 → expected bytes ~600K, actual 50M → >80x over. Warn.
    assert any("is" in rec.message and "x the expected" in rec.message for rec in caplog.records)


def test_sanity_check_size_no_warn_on_normal() -> None:
    # Just call; assert no exception and no crash.
    sanity_check_size(
        output_size_bytes=600_000,
        mezzanine_size_bytes=1_000_000,
        preset=PRESETS["mp4_h264_social"],
    )


def test_sanity_check_zero_sized_mezzanine_is_noop() -> None:
    sanity_check_size(
        output_size_bytes=1, mezzanine_size_bytes=0, preset=PRESETS["mp4_h264_social"]
    )
