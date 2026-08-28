"""Auto-reframe: pick a moving crop window that follows the subject.

For portrait/square output from wider footage, a static center-crop loses the
action and letterboxing wastes the frame. This module samples a handful of
frames per clip and estimates where the subject is:

- motion energy (frame differencing) — works for sports/action footage;
- face detection (OpenCV Haar cascade) — dominates when faces are present,
  which suits talking-head content.

The result is deliberately simple: a start and end x-position (fractions of
source width). The clip filter then pans linearly between them — a slow,
intentional-looking camera move, not jittery per-frame tracking.

Deterministic: fixed sample times, pure array math.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

N_SAMPLES = 12
DETECT_WIDTH = 320  # downscale width for analysis
MAX_DRIFT = 0.25  # max |x1 - x0| so pans stay subtle
FACE_WEIGHT = 3.0  # face centroids count this much more than motion


def estimate_pan(
    source: Path, in_ts: float, out_ts: float
) -> tuple[float, float]:
    """Return (x0_frac, x1_frac): subject x-center at clip start/end as
    fractions of source width. Falls back to (0.5, 0.5) on any failure."""
    try:
        return _estimate(source, in_ts, out_ts)
    except Exception:
        log.warning("auto-reframe estimation failed for %s", source, exc_info=True)
        return (0.5, 0.5)


def _estimate(source: Path, in_ts: float, out_ts: float) -> tuple[float, float]:
    import cv2
    import numpy as np

    from reelforge_core.vision import frame_diff_profile, grey_small

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return (0.5, 0.5)
    try:
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        duration = max(0.1, out_ts - in_ts)
        times = [in_ts + duration * (i + 0.5) / N_SAMPLES for i in range(N_SAMPLES)]

        grays: list["np.ndarray"] = []
        face_xs: list[tuple[int, float]] = []  # (sample_index, x_frac)
        for i, t in enumerate(times):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                grays.append(None)  # type: ignore[arg-type]
                continue
            gray = grey_small(frame, DETECT_WIDTH)
            grays.append(gray)
            if i % 3 == 0 and not cascade.empty():
                faces = cascade.detectMultiScale(gray, 1.2, 4, minSize=(16, 16))
                if len(faces):
                    # Largest face wins.
                    fx, _, fw, _ = max(faces, key=lambda f: f[2] * f[3])
                    face_xs.append((i, (fx + fw / 2.0) / gray.shape[1]))

        # Motion centroids from consecutive-sample differences.
        motion_xs: list[tuple[int, float]] = []
        prev = None
        for i, g in enumerate(grays):
            if g is None:
                continue
            if prev is not None and prev.shape == g.shape:
                col, _ = frame_diff_profile(prev, g)
                total = col.sum()
                if total > 1e-3:
                    centroid = float((col * np.arange(col.size)).sum() / total)
                    motion_xs.append((i, centroid / col.size))
            prev = g

        weighted: list[tuple[int, float, float]] = [
            (i, x, 1.0) for i, x in motion_xs
        ] + [(i, x, FACE_WEIGHT) for i, x in face_xs]
        if not weighted:
            return (0.5, 0.5)

        half = N_SAMPLES / 2.0

        def _avg(items: list[tuple[int, float, float]]) -> float | None:
            tw = sum(w for _, _, w in items)
            if tw <= 0:
                return None
            return sum(x * w for _, x, w in items) / tw

        first = _avg([it for it in weighted if it[0] < half])
        second = _avg([it for it in weighted if it[0] >= half])
        overall = _avg(weighted) or 0.5
        x0 = first if first is not None else overall
        x1 = second if second is not None else overall

        # Keep the pan subtle.
        if abs(x1 - x0) > MAX_DRIFT:
            mid = (x0 + x1) / 2.0
            x0 = mid + (MAX_DRIFT / 2.0 if x0 > x1 else -MAX_DRIFT / 2.0)
            x1 = mid - (MAX_DRIFT / 2.0 if x0 > x1 else -MAX_DRIFT / 2.0)
        return (round(min(max(x0, 0.0), 1.0), 4), round(min(max(x1, 0.0), 1.0), 4))
    finally:
        cap.release()


def should_crop(
    source_w: int, source_h: int, target_w: int, target_h: int, mode: str
) -> bool:
    """Crop-track applies when the source is meaningfully wider than the
    target aspect (portrait/square output from landscape footage)."""
    if mode == "letterbox":
        return False
    if source_w <= 0 or source_h <= 0:
        return False
    source_ar = source_w / source_h
    target_ar = target_w / target_h
    if mode == "crop":
        return source_ar > target_ar * 1.01
    # mode == "auto": crop only for portrait/square targets.
    return target_ar <= 1.0 and source_ar > target_ar * 1.2
