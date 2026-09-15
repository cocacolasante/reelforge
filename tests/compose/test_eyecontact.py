"""Eye-contact correction: geometry, gaze planning, the eye warp, graceful
failure, and cache-key compatibility."""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from reelforge_core.compose import eyecontact as ec
from reelforge_core.compose.clips import _eye_key
from reelforge_core.models import ComposeConfig

CENTERS = {"R": (300.0, 400.0), "L": (500.0, 400.0)}
IRIS_IDX = {"R": (468, (469, 470, 471, 472)), "L": (473, (474, 475, 476, 477))}


def _face(iris_dx: float = 0.0, iris_dy: float = 0.0, open_scale: float = 1.0) -> np.ndarray:
    """478 synthetic face-mesh points: each eye's 16-point opening on an
    ellipse (100 x 40 px), iris centre + ring (r=15) offset by (dx, dy)."""
    pts = np.zeros((478, 2))
    for name, (contour, *_rest) in ec.EYES.items():
        cx, cy = CENTERS[name]
        for k, idx in enumerate(contour):
            a = 2 * math.pi * k / len(contour)
            pts[idx] = (cx + 50 * math.cos(a), cy + 20 * open_scale * math.sin(a))
        center, ring = IRIS_IDX[name]
        pts[center] = (cx + iris_dx, cy + iris_dy)
        for j, idx in enumerate(ring):
            a = math.pi / 2 * j
            pts[idx] = (cx + iris_dx + 15 * math.cos(a), cy + iris_dy + 15 * math.sin(a))
    return pts


def _geoms(**kw) -> dict:
    pts = _face(**kw)
    return {e: ec.eye_geometry(pts, e) for e in ("R", "L")}


def _ref() -> dict:
    g = _geoms()
    return {f"{e}_{k}": g[e][k] for e in ("R", "L") for k in ("u", "v", "open")}


# ---- geometry + planning -------------------------------------------------------


def test_eye_geometry_measures_iris_offset_in_eye_widths():
    g = ec.eye_geometry(_face(iris_dx=10.0, iris_dy=-4.0), "R")
    assert g["w"] == pytest.approx(100.0)
    assert g["u"] == pytest.approx(0.10)
    assert g["v"] == pytest.approx(-0.04)
    assert g["r"] == pytest.approx(15.0)


def test_gaze_plan_corrects_caps_and_gates():
    ref = _ref()
    frames = [_geoms(iris_dx=8.0)] * 10 + [_geoms(iris_dx=30.0)] * 10 + [_geoms(open_scale=0.2)] * 10 + [None] * 10
    yaws = [0.0] * 30 + [0.0] * 10
    du, dv, gate = ec.gaze_plan(frames, yaws, ref)
    assert du[5] == pytest.approx(-0.08) and gate[5] == pytest.approx(1.0)
    assert du[15] == pytest.approx(-ec.MAX_DU)  # big glance: capped, not erased
    assert gate[25] == pytest.approx(0.0)  # eyes nearly closed
    assert gate[35] == pytest.approx(0.0)  # no face
    _, _, gate_turned = ec.gaze_plan([_geoms(iris_dx=8.0)] * 9, [35.0] * 9, ref)
    assert np.all(gate_turned == 0.0)  # head turned away


def test_smooth_is_zero_lag_and_length_preserving():
    x = np.array([0.0] * 10 + [1.0] * 10)
    y = ec.smooth(x)
    assert len(y) == len(x)
    assert y[0] == pytest.approx(0.0) and y[-1] == pytest.approx(1.0)
    assert y[9] == pytest.approx(3 / 7) and y[10] == pytest.approx(4 / 7)


# ---- the warp ------------------------------------------------------------------


def test_warp_moves_the_iris_and_leaves_skin_alone():
    import cv2

    frame = np.full((600, 800, 3), 200, np.uint8)
    cv2.circle(frame, (310, 400), 13, (40, 40, 40), -1)  # iris sitting 10px right
    before = frame.copy()
    g = _geoms(iris_dx=10.0)["R"]
    ec.warp_eye(frame, g, np.array([-10.0, 0.0]))

    def dark_cx(img):
        ys, xs = np.nonzero(img[360:440, 240:360, 0] < 100)
        return 240 + xs.mean()

    assert dark_cx(before) == pytest.approx(310, abs=0.5)
    assert dark_cx(frame) < 305  # pulled toward centre
    assert np.array_equal(frame[330:350, :], before[330:350, :])  # above the lid
    assert np.array_equal(frame[:, 400:450], before[:, 400:450])  # between the eyes


def test_tiny_shifts_are_skipped():
    frame = np.full((600, 800, 3), 200, np.uint8)
    before = frame.copy()
    ec.warp_eye(frame, _geoms()["R"], np.array([0.1, 0.0]))
    assert np.array_equal(frame, before)


# ---- failure + cache -----------------------------------------------------------


async def test_missing_model_never_raises_and_leaves_clip(tmp_path: Path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really a video")
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    monkeypatch.setenv("REELFORGE_FACE_MODEL", str(tmp_path / "missing.task"))
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(tmp_path / "data"))
    res = await ec.apply_eye_contact(clip, "asset1", src, ComposeConfig(eye_contact=True))
    assert res["applied"] is False and res["error"]
    assert clip.read_bytes() == b"not really a video"


def test_eye_key_only_present_when_on():
    assert _eye_key(ComposeConfig()) == {}
    assert _eye_key(ComposeConfig(eye_contact=True)) == {"eye_contact": ec.EYE_CONTACT_VERSION}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
def test_correct_clip_warps_and_reencodes(tmp_path: Path, monkeypatch):
    """The full two-pass path with the landmarker faked out: pass 2 must warp,
    encode and replace the clip (the first live run crashed right here)."""
    import cv2

    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=800x600:d=1:r=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)],
        check=True,
    )
    before = clip.read_bytes()

    class _Dummy:
        def close(self):
            pass

    monkeypatch.setattr(ec, "_landmarker", lambda video: _Dummy())
    monkeypatch.setattr(ec, "_detect", lambda lm, frame, ts: (_geoms(iris_dx=8.0), 0.0))
    stats = ec.correct_clip(clip, _ref(), ComposeConfig())
    assert stats["applied"] is True and stats["faces"] == stats["frames"] > 0
    assert stats["max_px"] == pytest.approx(8.0, abs=0.5)
    assert clip.read_bytes() != before
    cap = cv2.VideoCapture(str(clip))
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == stats["frames"]
    cap.release()
    assert not list(tmp_path.glob("*.eye.*"))  # no temp leftovers
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(clip)],
                           capture_output=True, text=True, check=True).stdout.split()
    assert "audio" in probe  # the clip's own audio survives


def _mediapipe_ready() -> bool:
    try:
        ec._landmarker(video=False).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
def test_faceless_clip_is_left_untouched(tmp_path: Path):
    if not _mediapipe_ready():
        pytest.skip("mediapipe / face model not installed")
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=360x640:d=1:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    before = clip.read_bytes()
    assert ec.measure_reference(clip) is None
    stats = ec.correct_clip(clip, _ref(), ComposeConfig())
    assert stats["faces"] == 0 and stats["applied"] is False
    assert clip.read_bytes() == before
