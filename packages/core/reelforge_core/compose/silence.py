"""Speech-activity envelope: where the audio is actually silent.

Jump cuts and beat-sync trims used to trust Whisper's word timestamps, which
mark words as ending before their sound does (live 2026-09-14: "automatically,"
kept sounding 0.41s past its end timestamp, so every jump cut after it clipped
the word) and paper over pauses the transcript mis-times. This module reads the
analysis audio (`working/{asset_id}/audio.wav`, mono 16 kHz PCM) into a 20 ms
RMS envelope and answers "is this stretch silent?" against an adaptive
threshold: the clip's own noise floor plus a margin.
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from reelforge_core.models import Transcript

log = logging.getLogger(__name__)

HOP_SEC = 0.02
NOISE_FLOOR_PERCENTILE = 10
# Sound this far above the noise floor counts as not-silence. Tuned on a
# talking-head recording (floor -55 dB, speech median -38 dB): +12 dB keeps
# room tone and breaths silent while word tails stay audible.
SILENCE_MARGIN_DB = 12.0
# Nothing this loud is ever silence, however noisy the room.
SILENCE_CEILING_DB = -30.0
# Without audio, a trim may eat this far past the last transcribed word's end
# timestamp (Whisper word ends run early — see the module docstring).
TRANSCRIPT_TAIL_PAD_SEC = 0.4
# Silence a trim always leaves at the end of a shot.
TRIM_KEEP_SEC = 0.05


@dataclass(frozen=True)
class SpeechEnvelope:
    db: np.ndarray  # per-hop RMS level in dBFS
    threshold_db: float
    hop_sec: float = HOP_SEC

    @property
    def duration(self) -> float:
        return len(self.db) * self.hop_sec

    def silent_runs(self, start: float, end: float, min_len: float) -> list[tuple[float, float]]:
        """Maximal silent stretches of at least `min_len` seconds within
        [start, end], clipped to it. Ascending."""
        i0 = max(0, int(start / self.hop_sec))
        i1 = min(len(self.db), int(np.ceil(end / self.hop_sec)))
        if i1 <= i0:
            return []
        quiet = self.db[i0:i1] < self.threshold_db
        # Run boundaries via diff on a zero-padded mask.
        edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.astype(np.int8), [0]))))
        runs: list[tuple[float, float]] = []
        for a, b in zip(edges[::2], edges[1::2]):
            s = max(start, (i0 + a) * self.hop_sec)
            e = min(end, (i0 + b) * self.hop_sec)
            if e - s >= min_len - 1e-9:
                runs.append((round(float(s), 3), round(float(e), 3)))
        return runs

    def trailing_silence(self, start: float, end: float) -> float:
        """Seconds of continuous silence ending at `end` (never past `start`)."""
        i0 = max(0, int(start / self.hop_sec))
        i1 = min(len(self.db), int(end / self.hop_sec))
        k = i1
        while k > i0 and self.db[k - 1] < self.threshold_db:
            k -= 1
        return (i1 - k) * self.hop_sec


def envelope_from_samples(samples: np.ndarray, sample_rate: int) -> SpeechEnvelope | None:
    """20 ms RMS envelope of float samples in [-1, 1]. Pure."""
    hop = max(1, int(round(sample_rate * HOP_SEC)))
    n = len(samples) // hop
    if n == 0:
        return None
    frames = samples[: n * hop].astype(np.float32).reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms)
    floor = float(np.percentile(db, NOISE_FLOOR_PERCENTILE))
    threshold = min(floor + SILENCE_MARGIN_DB, SILENCE_CEILING_DB)
    return SpeechEnvelope(db=db, threshold_db=threshold, hop_sec=hop / sample_rate)


def load_speech_envelope(wav_path: Path) -> SpeechEnvelope | None:
    """Envelope of a 16-bit PCM WAV (the analysis audio.wav). None when the
    file is missing or unreadable — callers fall back to the transcript."""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getsampwidth() != 2:
                return None
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    return envelope_from_samples(samples, sample_rate)


def speech_free_tail(
    start: float,
    end: float,
    envelope: SpeechEnvelope | None = None,
    transcript: Transcript | None = None,
) -> float | None:
    """How many seconds can be trimmed off the END of [start, end] without
    cutting speech. None when nothing is known about speech there."""
    if envelope is not None:
        return max(0.0, envelope.trailing_silence(start, end) - TRIM_KEEP_SEC)
    if transcript is not None:
        last_end = max(
            (w.end for seg in transcript.segments for w in seg.words if w.start < end and w.end > start),
            default=None,
        )
        if last_end is None:
            return end - start
        return max(0.0, end - (last_end + TRANSCRIPT_TAIL_PAD_SEC))
    return None
