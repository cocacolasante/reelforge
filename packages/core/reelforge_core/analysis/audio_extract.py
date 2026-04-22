"""Extract mono 16 kHz PCM to a single audio.wav shared by transcribe + loudness."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from reelforge_core.errors import TranscriptionError

log = logging.getLogger(__name__)


def extract_audio(source: Path, out_wav: Path) -> Path:
    """Produce `out_wav` from `source`. Idempotent: re-runs if the WAV is missing/empty."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if out_wav.exists() and out_wav.stat().st_size > 0:
        return out_wav
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    log.info("extracting audio: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise TranscriptionError(f"audio extraction failed: {result.stderr.strip()}")
    if not out_wav.exists() or out_wav.stat().st_size == 0:
        raise TranscriptionError("audio extraction produced empty file")
    return out_wav
