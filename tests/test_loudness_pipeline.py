"""Loudness against REAL ffmpeg output, plus the resume-stamp versioning.

The parser tests feed hand-written lines, which hid a regression where
ebur128's per-frame lines were logged below ffmpeg's default loglevel: every
loudness bin of every analyzed clip was the -80 silence sentinel.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reelforge_core.analysis import audio as audio_mod
from reelforge_core.analysis import pipeline as analysis_pipeline
from reelforge_core.analysis.audio import LOUDNESS_VERSION, NEG_INF, measure_loudness
from reelforge_core.errors import LoudnessError
from reelforge_core.ingest import MediaAsset
from reelforge_core.models import AnalysisConfig, noop_progress


def _silence_then_tone(out: Path) -> Path:
    """4s clip: 2s digital silence, then 2s of a 440 Hz tone at -6 dBFS peak."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=4:r=25",
            "-f", "lavfi", "-i",
            "aevalsrc=exprs='if(gte(t,2),0.5*sin(2*PI*440*t),0)':s=48000:d=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


async def test_measure_loudness_reads_real_levels(tmp_path: Path) -> None:
    asset = MediaAsset.from_path(_silence_then_tone(tmp_path / "tone.mp4"))
    wd = tmp_path / "wd"
    wd.mkdir()
    points = await measure_loudness(asset, wd, AnalysisConfig(), noop_progress)

    assert len(points) >= 4
    # The silent half reads as (or right at) the sentinel...
    assert points[0].lufs == NEG_INF
    assert points[1].lufs < -60.0
    # ...and the tone half reads a real level — the regression made it -80 too.
    assert points[3].lufs > -20.0
    assert (wd / "loudness.json").exists()
    assert not (wd / "ebur128.stderr.log").exists()


async def test_zero_parseable_lines_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = MediaAsset.from_path(_silence_then_tone(tmp_path / "tone.mp4"))
    wd = tmp_path / "wd"
    wd.mkdir()

    def _unparseable(wav_path: Path, log_path: Path) -> None:
        log_path.write_text("ffmpeg printed nothing the parser recognizes\n")

    monkeypatch.setattr(audio_mod, "_run_ebur128", _unparseable)
    with pytest.raises(LoudnessError, match="no parseable lines"):
        await measure_loudness(asset, wd, AnalysisConfig(), noop_progress)
    # No flat -80 track written; the log is kept for forensics.
    assert not (wd / "loudness.json").exists()
    assert (wd / "ebur128.stderr.log").exists()


def test_pre_fix_loudness_stamp_forces_recompute(tmp_path: Path) -> None:
    p = tmp_path / "loudness.json"
    p.write_text("[]")
    analysis_pipeline._write_stamp(p, {"source_mtime": 1.0})  # pre-fix shape
    assert not analysis_pipeline._stamp_matches(p, analysis_pipeline._loudness_stamp(1.0))

    analysis_pipeline._write_stamp(p, analysis_pipeline._loudness_stamp(1.0))
    assert analysis_pipeline._stamp_matches(p, analysis_pipeline._loudness_stamp(1.0))


def test_energy_stamp_tracks_loudness_version(tmp_path: Path) -> None:
    """Energy's loudness_delta half derives from loudness.json, so a loudness
    fix must recompute energy too."""
    cfg = AnalysisConfig()
    p = tmp_path / "energy.json"
    p.write_text("[]")
    analysis_pipeline._write_stamp(
        p, {"source_mtime": 1.0, "sample_fps": cfg.energy_sample_fps}  # pre-fix shape
    )
    expected = analysis_pipeline._energy_stamp(1.0, cfg)
    assert expected["loudness_version"] == LOUDNESS_VERSION
    assert not analysis_pipeline._stamp_matches(p, expected)
