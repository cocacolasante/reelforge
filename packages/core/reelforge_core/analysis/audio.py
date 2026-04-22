"""LUFS loudness curve via ffmpeg's ebur128 filter. 1-second bins, mean of momentary."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import subprocess
from pathlib import Path
from typing import Iterable

from reelforge_core.analysis.audio_extract import extract_audio
from reelforge_core.errors import LoudnessError
from reelforge_core.ingest import MediaAsset
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    AnalysisConfig,
    LoudnessPoint,
    ProgressCallback,
    ProgressEvent,
    compute_overall,
)

log = logging.getLogger(__name__)

EBUR128_LINE = re.compile(r"t:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+|-inf)")
NEG_INF = -80.0


def bin_loudness(
    samples: Iterable[tuple[float, float]], duration_sec: float
) -> list[LoudnessPoint]:
    """Bin `(t, lufs)` samples into 1-second bins centered at 0.5, 1.5, ..."""
    if duration_sec <= 0:
        return []
    n_bins = max(1, math.ceil(duration_sec))
    sums: list[float] = [0.0] * n_bins
    counts: list[int] = [0] * n_bins
    for t, m in samples:
        idx = int(math.floor(t))
        if idx < 0 or idx >= n_bins:
            continue
        sums[idx] += m
        counts[idx] += 1
    points: list[LoudnessPoint] = []
    for i in range(n_bins):
        mean = sums[i] / counts[i] if counts[i] > 0 else NEG_INF
        points.append(LoudnessPoint(time_sec=i + 0.5, lufs=mean))
    return points


def parse_ebur128_stderr(stream: Iterable[str]) -> list[tuple[float, float]]:
    """Parse ffmpeg -filter_complex ebur128 verbose lines to `(t, momentary_lufs)`."""
    out: list[tuple[float, float]] = []
    for line in stream:
        m = EBUR128_LINE.search(line)
        if not m:
            continue
        t = float(m.group(1))
        raw = m.group(2)
        lufs = NEG_INF if raw == "-inf" else float(raw)
        out.append((t, lufs))
    return out


def _run_ebur128(wav_path: Path, log_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(wav_path),
        "-filter_complex",
        "ebur128=peak=true:framelog=verbose",
        "-f",
        "null",
        "-",
    ]
    log.info("ebur128: %s", " ".join(cmd))
    # We only need stderr; stdout goes to the null muxer.
    with log_path.open("wb") as err:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=err, check=False)
    if result.returncode != 0:
        raise LoudnessError(f"ffmpeg ebur128 exited {result.returncode}")


async def measure_loudness(
    asset: MediaAsset,
    working_dir: Path,
    config: AnalysisConfig,
    progress: ProgressCallback,
) -> list[LoudnessPoint]:
    if not asset.has_audio:
        write_json_atomic(working_dir / "loudness.json", [])
        await progress(ProgressEvent("loudness", 1.0, compute_overall("loudness", 1.0)))
        return []

    wav_path = working_dir / "audio.wav"
    await asyncio.to_thread(extract_audio, asset.path, wav_path)
    await progress(ProgressEvent("loudness", 0.1, compute_overall("loudness", 0.1)))

    log_path = working_dir / "ebur128.stderr.log"
    await asyncio.to_thread(_run_ebur128, wav_path, log_path)
    await progress(ProgressEvent("loudness", 0.6, compute_overall("loudness", 0.6)))

    # Stream the log line-by-line. ebur128 output can be tens of MB for long audio.
    def _read_and_parse() -> list[LoudnessPoint]:
        samples: list[tuple[float, float]] = []
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            samples = parse_ebur128_stderr(f)
        points = bin_loudness(samples, asset.probe.duration_s)
        log_path.unlink(missing_ok=True)
        return points

    points = await asyncio.to_thread(_read_and_parse)
    write_json_atomic(working_dir / "loudness.json", [p.model_dump() for p in points])
    await progress(ProgressEvent("loudness", 1.0, compute_overall("loudness", 1.0)))
    return points
