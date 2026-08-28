"""Shared OpenCV frame helpers (lazy cv2 import — cv2 is a heavy optional dep).

Used by compose/reframe.py (subject-tracked crop) and analysis/energy.py
(per-second motion track). Both need the same downscaled-grey frame-diff; the
math lives here once so the two stay in agreement.
"""

from __future__ import annotations

DETECT_WIDTH = 320  # downscale width for analysis


def grey_small(frame, width: int = DETECT_WIDTH):
    """Downscale a BGR frame to `width` and convert to grayscale."""
    import cv2

    h, w = frame.shape[:2]
    scale = width / w
    small = cv2.resize(frame, (width, max(2, int(h * scale))))
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def frame_diff_profile(prev, cur):
    """Absolute difference of two same-shape grey frames.

    Returns (column_profile, n_pixels): the per-column sum of |cur - prev|
    (reframe derives its motion centroid from it; its total is the raw
    motion energy) and the pixel count (so callers can normalize to a
    0..255 mean-abs-diff scale).
    """
    import cv2
    import numpy as np

    diff = cv2.absdiff(cur, prev).astype(np.float64)
    return diff.sum(axis=0), diff.size
