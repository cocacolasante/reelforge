"""Beat-sync trim math, beat detection on a synthetic click track, reframe
gating, and the crop-track clip filter."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from reelforge_core.compose.beats import (
    BeatGrid,
    compute_beat_end_trims,
    detect_beats,
)
from reelforge_core.compose.clips import build_clip_command
from reelforge_core.compose.reframe import estimate_pan, should_crop
from reelforge_core.models import ComposeConfig


# ---- BeatGrid --------------------------------------------------------------


def test_phase_within_beat():
    g = BeatGrid(bpm=120.0, phase_sec=0.1)
    assert g.interval == 0.5
    assert g.phase_within_beat(0.1) == pytest.approx(0.0)
    assert g.phase_within_beat(0.85) == pytest.approx(0.25)


# ---- compute_beat_end_trims ------------------------------------------------


def test_trims_align_transitions_to_beats():
    g = BeatGrid(bpm=120.0, phase_sec=0.0)  # beats every 0.5s
    trims = compute_beat_end_trims([10.0, 10.0, 10.0], 0.4, g, max_trim=0.45)
    # Transition 0 center: (10 - 0.4) + 0.2 = 9.8 -> 0.3 past a beat.
    assert trims[0] == pytest.approx(0.3)
    # After trimming clip 0: transition 1 center = (9.7 + 10) - 0.8 + 0.2 = 19.1
    # -> 0.1 past a beat.
    assert trims[1] == pytest.approx(0.1)


def test_trims_respect_cap():
    g = BeatGrid(bpm=120.0, phase_sec=0.0)
    trims = compute_beat_end_trims([10.0, 10.0, 10.0], 0.4, g, max_trim=0.2)
    # 0.3 > cap -> skipped; untrimmed transition 1 center = 19.4 -> 0.4 > cap.
    assert trims == [0.0, 0.0]


def test_trims_never_shrink_clip_below_minimum():
    g = BeatGrid(bpm=120.0, phase_sec=0.0)
    # Clip 0 is barely above the minimum — trimming would cross it.
    trims = compute_beat_end_trims([1.5, 10.0], 0.4, g, max_trim=0.45)
    assert trims[0] == 0.0


def test_single_clip_no_trims():
    g = BeatGrid(bpm=120.0, phase_sec=0.0)
    assert compute_beat_end_trims([10.0], 0.4, g, max_trim=0.45) == []


# ---- detect_beats on a synthetic click track -------------------------------


def _write_click_track(path: Path, bpm: float, offset: float, duration: float) -> None:
    import numpy as np

    sr = 22050
    n = int(sr * duration)
    samples = np.zeros(n, dtype=np.float64)
    interval = 60.0 / bpm
    t = offset
    while t < duration:
        start = int(t * sr)
        burst = np.random.RandomState(int(t * 1000)).uniform(-1, 1, int(0.05 * sr))
        end = min(n, start + burst.size)
        samples[start:end] += burst[: end - start]
        t += interval
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())


def test_detect_beats_click_track(tmp_path: Path):
    wav = tmp_path / "clicks.wav"
    _write_click_track(wav, bpm=120.0, offset=0.25, duration=20.0)
    grid = detect_beats(wav)
    assert grid is not None
    # Beat interval ~0.5s; lag quantization is HOP/22050 ≈ 23ms per frame.
    assert grid.interval == pytest.approx(0.5, abs=0.03)
    # Phase should land near the click offset (within ~2 analysis frames).
    assert grid.phase_within_beat(0.25) < 0.08 or grid.phase_within_beat(0.25) > (
        grid.interval - 0.08
    )


def test_detect_beats_silence_returns_none(tmp_path: Path):
    import numpy as np

    wav = tmp_path / "silence.wav"
    pcm = np.zeros(22050 * 10, dtype="<i2")
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(22050)
        f.writeframes(pcm.tobytes())
    assert detect_beats(wav) is None


# ---- reframe gating --------------------------------------------------------


def test_should_crop_landscape_to_portrait_auto():
    assert should_crop(1920, 1080, 1080, 1920, "auto") is True


def test_should_crop_landscape_to_landscape_auto():
    assert should_crop(1920, 1080, 1920, 1080, "auto") is False


def test_should_crop_letterbox_mode_never():
    assert should_crop(1920, 1080, 1080, 1920, "letterbox") is False


def test_should_crop_crop_mode_any_wider():
    assert should_crop(1920, 1080, 1920, 1080, "crop") is False  # same AR
    assert should_crop(2560, 1080, 1920, 1080, "crop") is True  # ultrawide


def test_should_crop_portrait_source_no_crop():
    assert should_crop(1080, 1920, 1080, 1920, "auto") is False


def test_estimate_pan_missing_file_falls_back_centered():
    assert estimate_pan(Path("/nonexistent.mp4"), 0.0, 10.0) == (0.5, 0.5)


# ---- crop-track clip filter ------------------------------------------------


def test_clip_command_pan_uses_crop_not_pad():
    cmd = build_clip_command(
        source=Path("/src.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=10.0,
        out_ts=20.0,
        config=ComposeConfig(aspect="9:16"),
        has_audio=True,
        is_hdr=False,
        pan=(0.4, 0.6),
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop=w=floor(ih*1080/1920/2)*2:h=ih" in vf
    assert "pad=" not in vf
    # Pan drifts using source-time progress over the clip window.
    assert "(t-10.000)/10.000" in vf
    assert "0.4000+(0.2000)" in vf
    assert "scale=1080:1920" in vf


def test_clip_command_no_pan_keeps_letterbox():
    cmd = build_clip_command(
        source=Path("/src.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=0.0,
        out_ts=10.0,
        config=ComposeConfig(aspect="9:16"),
        has_audio=True,
        is_hdr=False,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "pad=1080:1920" in vf
    assert "crop=" not in vf
