"""Beat detection + transition alignment.

`detect_beats` estimates a music track's tempo AND phase (time of the first
beat) from its audio, so transitions can land on actual beats — BPM metadata
alone can't give phase. The estimator is intentionally simple and fully
deterministic: onset strength from frame-energy flux, tempo from
autocorrelation over a plausible BPM range, phase from the best-aligned comb.
Accurate enough for aligning a handful of transitions; not a DJ tool.

`compute_beat_end_trims` then shortens each interior clip by up to a cap so
that every crossfade's midpoint lands on the nearest earlier beat.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 22050
HOP = 512
MIN_BPM = 60.0
MAX_BPM = 180.0


@dataclass(frozen=True)
class BeatGrid:
    bpm: float
    phase_sec: float  # time of the first beat

    @property
    def interval(self) -> float:
        return 60.0 / self.bpm

    def phase_within_beat(self, t: float) -> float:
        """Seconds since the most recent beat at time `t`."""
        return (t - self.phase_sec) % self.interval


def _decode_mono(path: Path, max_sec: float) -> "np.ndarray":  # noqa: F821
    import numpy as np

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-t", f"{max_sec:.1f}",
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def detect_beats(path: Path, analyze_sec: float = 60.0) -> BeatGrid | None:
    """Estimate (bpm, phase) for a music file. Returns None when the audio is
    too short / too flat to measure (silence, drones)."""
    import numpy as np

    try:
        samples = _decode_mono(path, analyze_sec)
    except Exception:
        log.warning("beat detection: could not decode %s", path, exc_info=True)
        return None
    if samples.size < SAMPLE_RATE * 5:
        return None

    # Onset envelope: rectified frame-energy flux.
    n_frames = samples.size // HOP
    frames = samples[: n_frames * HOP].reshape(n_frames, HOP)
    energy = (frames.astype(np.float64) ** 2).sum(axis=1)
    flux = np.maximum(0.0, np.diff(energy))
    if flux.max() <= 0:
        return None
    flux = flux / flux.max()
    # Smooth so an onset spike spans a few frames: beat intervals are rarely
    # an integer number of frames, and without smoothing the autocorrelation
    # peak splits across adjacent lags (which favors tempo octaves).
    flux = np.convolve(flux, np.array([0.25, 0.5, 1.0, 0.5, 0.25]), mode="same")

    frame_dt = HOP / SAMPLE_RATE
    min_lag = int(round((60.0 / MAX_BPM) / frame_dt))
    max_lag = int(round((60.0 / MIN_BPM) / frame_dt))
    if max_lag >= flux.size // 2:
        return None

    # Tempo: autocorrelation peak in the plausible lag range. Tempo octaves
    # (60 vs 120 BPM) produce near-equal peaks at lag and 2*lag — prefer the
    # SMALLEST near-max lag: the finer grid contains the slower one's beats,
    # and finer beats mean smaller transition trims.
    ac = np.correlate(flux, flux, mode="full")[flux.size - 1 :]
    window = ac[min_lag : max_lag + 1]
    peak = float(window.max())
    if peak <= 0:
        return None
    candidates = np.nonzero(window >= 0.8 * peak)[0]
    lag = int(candidates[0]) + min_lag
    bpm = 60.0 / (lag * frame_dt)

    # Phase: comb offset that best aligns with onsets.
    scores = [float(flux[off::lag].sum()) for off in range(lag)]
    phase = int(np.argmax(np.asarray(scores))) * frame_dt
    return BeatGrid(bpm=round(bpm, 2), phase_sec=round(phase, 4))


def compute_beat_end_trims(
    durations: list[float],
    xfade_dur: float,
    grid: BeatGrid,
    max_trim: float,
    min_clip_sec: float = 1.0,
) -> list[float]:
    """Per-clip end trims (len n-1; clips 0..n-2) that put each crossfade's
    midpoint on a beat. Sequential: earlier trims shift later transitions.
    A transition whose beat is further than `max_trim` away is left alone.
    """
    n = len(durations)
    trims = [0.0] * max(0, n - 1)
    if n < 2:
        return trims
    adjusted = list(durations)
    for k in range(n - 1):
        # Transition k midpoint on the mezzanine timeline (graph_builder's
        # xfade offset + half the fade).
        offset = sum(adjusted[: k + 1]) - (k + 1) * xfade_dur
        center = offset + xfade_dur / 2.0
        trim = grid.phase_within_beat(center)
        if trim <= 1e-4 or trim > max_trim:
            continue
        if adjusted[k] - trim < min_clip_sec + xfade_dur:
            continue
        trims[k] = round(trim, 3)
        adjusted[k] -= trims[k]
    return trims
