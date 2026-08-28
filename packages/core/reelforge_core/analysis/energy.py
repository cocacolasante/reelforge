"""Per-second energy track: visual motion + loudness delta.

Feeds the moment-anchored candidate generator (Selection v2): action footage
without speech or hard cuts still has *events* — a jump, a crash, a crowd
roar — that show up as motion spikes and loudness jumps.

Motion is the mean absolute frame difference of downscaled grey frames,
sampled at ~`AnalysisConfig.energy_sample_fps` via SEQUENTIAL decode
(grab/retrieve with a frame stride — per-second seeks are far too slow on
long assets). Bins are 1 second wide with centers at i + 0.5, matching the
loudness bin convention in analysis/audio.py.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path

from reelforge_core.ingest import MediaAsset
from reelforge_core.models import (
    AnalysisConfig,
    EnergyPoint,
    LoudnessPoint,
)

log = logging.getLogger(__name__)

# At or below this LUFS a bin is the -80.0 silence sentinel, not a level —
# deltas against it are meaningless and are clamped to 0.
SILENCE_LUFS = -79.9


def _motion_track(source: Path, duration_sec: float, sample_fps: float) -> list[float]:
    """Mean abs frame diff per 1s bin (0..255 scale). Zeros on any failure."""
    import cv2

    from reelforge_core.vision import frame_diff_profile, grey_small

    n_bins = max(1, math.ceil(duration_sec))
    sums = [0.0] * n_bins
    counts = [0] * n_bins
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return [0.0] * n_bins
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if fps <= 0:
            fps = 30.0
        stride = max(1, int(round(fps / max(0.1, sample_fps))))
        prev = None
        idx = 0
        while True:
            if not cap.grab():
                break
            if idx % stride == 0:
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    break
                g = grey_small(frame)
                if prev is not None and prev.shape == g.shape:
                    col, n_px = frame_diff_profile(prev, g)
                    b = min(n_bins - 1, int(idx / fps))
                    sums[b] += float(col.sum()) / n_px
                    counts[b] += 1
                prev = g
            idx += 1
    finally:
        cap.release()
    return [sums[i] / counts[i] if counts[i] else 0.0 for i in range(n_bins)]


def loudness_deltas(loudness: list[LoudnessPoint], n_bins: int) -> list[float]:
    """LUFS(t) - LUFS(t-1) per bin; 0.0 at t=0, for missing bins, and when
    either side is the silence sentinel. Pure."""
    by_bin = {int(p.time_sec): p.lufs for p in loudness}  # center i+0.5 -> i
    out = [0.0] * n_bins
    for i in range(1, n_bins):
        cur = by_bin.get(i)
        prev = by_bin.get(i - 1)
        if cur is None or prev is None or cur <= SILENCE_LUFS or prev <= SILENCE_LUFS:
            continue
        out[i] = cur - prev
    return out


async def compute_energy(
    asset: MediaAsset,
    loudness: list[LoudnessPoint],
    config: AnalysisConfig,
) -> list[EnergyPoint]:
    """One EnergyPoint per second of the asset. Motion decode runs off the
    event loop; failures degrade to a zero track rather than failing analysis."""
    duration = asset.probe.duration_s
    try:
        motion = await asyncio.to_thread(
            _motion_track, asset.path, duration, config.energy_sample_fps
        )
    except Exception:
        log.warning("energy: motion track failed for %s", asset.path, exc_info=True)
        motion = [0.0] * max(1, math.ceil(duration))
    deltas = loudness_deltas(loudness, len(motion))
    return [
        EnergyPoint(
            time_sec=i + 0.5,
            motion=round(m, 4),
            loudness_delta=round(d, 3),
        )
        for i, (m, d) in enumerate(zip(motion, deltas))
    ]
