"""Eye-contact correction: nudge the irises toward the lens (local warp).

A speaker glancing at notes or a second screen reads as disengaged. For each
extracted video clip, MediaPipe FaceLandmarker finds both irises in every
frame. Each iris's offset from that speaker's own at-camera position — the
median over the source video, measured once and cached per asset — is
averaged across the two eyes, capped, faded out during blinks and head turns,
and smoothed over ~0.23s. Then only the inside of each eye opening is warped:
pixels near the iris shift fully, the shift fades to zero at the lids and
corners, so the sclera stretches on one side and compresses on the other.
Lids, lashes and skin never move.

Prototype findings (CP3, 2026-09-14, data/eyecontact-proto): clean up to
~0.12 eye-widths sideways — larger shifts leave a ghost of the original iris,
so big glances are softened, not erased; vertical correction is weak because
the lids follow the gaze. An iris-redraw (inpaint + paste) alternative looked
painted-on and was rejected.

Offsets are normalized to eye width, so the asset-level reference holds for
clips that were scaled, cropped (reframe) or retimed. Everything is
best-effort: a missing model, a clip without a face, or any error leaves the
clip untouched — eye contact must never fail a render.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from reelforge_core.models import ComposeConfig

log = logging.getLogger(__name__)

EYE_CONTACT_VERSION = "e1"
MAX_DU = 0.12  # eye widths, sideways
MAX_DV = 0.06  # eye widths, vertical
SMOOTH_FRAMES = 7
MIN_SHIFT_PX = 0.3
BLINK_OPEN_FRAC = 0.55  # openness vs reference where the correction is gone
BLINK_RAMP = 0.25
YAW_LIMIT_DEG = 20.0
YAW_RAMP_DEG = 5.0
REF_SAMPLE_FPS = 2.0
REF_MAX_FRAMES = 400
REF_MIN_FACES = 10
LANDMARK_MAX_WIDTH = 1280
DEFAULT_MODEL = "/app/assets/models/face_landmarker.task"

# name -> (eye-opening polygon, corner A, corner B, upper lid, lower lid)
EYES: dict[str, tuple[list[int], int, int, int, int]] = {
    "R": ([33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246], 33, 133, 159, 145),
    "L": ([362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398], 263, 362, 386, 374),
}
IRIS = ((468, (469, 470, 471, 472)), (473, (474, 475, 476, 477)))
REF_KEYS = ("R_u", "R_v", "L_u", "L_v", "R_open", "L_open")

_asset_locks: dict[str, asyncio.Lock] = {}


class EyeContactUnavailable(RuntimeError):
    """MediaPipe or its model isn't installed."""


def model_path() -> Path:
    return Path(os.environ.get("REELFORGE_FACE_MODEL", DEFAULT_MODEL))


def _landmarker(video: bool) -> Any:
    try:
        import mediapipe as mp  # noqa: F401
        from mediapipe.tasks.python import BaseOptions, vision
    except Exception as exc:  # ImportError, or the native lib failing to load
        raise EyeContactUnavailable(f"mediapipe unavailable: {exc}") from exc
    path = model_path()
    if not path.exists():
        raise EyeContactUnavailable(f"face landmarker model missing at {path}")
    opts = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(path)),
        running_mode=vision.RunningMode.VIDEO if video else vision.RunningMode.IMAGE,
        num_faces=1,
        output_facial_transformation_matrixes=True,
    )
    return vision.FaceLandmarker.create_from_options(opts)


# ---------------------------------------------------------------------------
# geometry + offsets (pure)
# ---------------------------------------------------------------------------


def eye_geometry(pts: np.ndarray, name: str) -> dict:
    """One eye's frame from 478 face-mesh points (pixel coords): opening
    polygon, width, axis, iris centre/radius, and the iris offset (u along
    the eye axis, v perpendicular — both in eye widths) plus openness."""
    contour, a, b, top, bot = EYES[name]
    p1, p2 = pts[a], pts[b]
    if p1[0] > p2[0]:
        p1, p2 = p2, p1
    c = (p1 + p2) / 2
    w = max(1e-6, float(np.linalg.norm(p2 - p1)))
    ax = (p2 - p1) / w
    perp = np.array([-ax[1], ax[0]])
    center_idx, ring = min(IRIS, key=lambda it: np.linalg.norm(pts[it[0]] - c))
    iris = pts[center_idx]
    d = iris - c
    return {
        "poly": pts[contour],
        "w": w,
        "ax": ax,
        "perp": perp,
        "iris": iris,
        "r": float(np.mean([np.linalg.norm(pts[k] - iris) for k in ring])),
        "u": float(d @ ax) / w,
        "v": float(d @ perp) / w,
        "open": float(np.linalg.norm(pts[top] - pts[bot])) / w,
    }


def yaw_degrees(matrix: Any) -> float:
    m = np.asarray(matrix)
    return math.degrees(math.atan2(m[0][2], m[2][2]))


def smooth(x: np.ndarray, k: int = SMOOTH_FRAMES) -> np.ndarray:
    """Centered moving average with edge padding (offline, zero-lag). Pure."""
    if len(x) < 2 or k < 2:
        return x
    pad = k // 2
    xp = np.pad(x, (pad, pad), mode="edge")
    return np.convolve(xp, np.ones(k) / k, mode="valid")[: len(x)]


def gaze_plan(
    geoms: list[dict | None], yaws: list[float], ref: dict[str, float]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-frame correction (du, dv in eye widths) and gate (0..1): both eyes
    averaged, capped, gated off for blinks / head turns / no face, smoothed.
    Pure."""
    n = len(geoms)
    du, dv, gate = np.zeros(n), np.zeros(n), np.zeros(n)
    for i, g in enumerate(geoms):
        if g is None:
            continue
        du[i] = np.mean([ref[f"{e}_u"] - g[e]["u"] for e in ("R", "L")])
        dv[i] = np.mean([ref[f"{e}_v"] - g[e]["v"] for e in ("R", "L")])
        openness = np.mean([g[e]["open"] / max(1e-6, ref[f"{e}_open"]) for e in ("R", "L")])
        g_blink = np.clip((openness - BLINK_OPEN_FRAC) / BLINK_RAMP, 0.0, 1.0)
        g_yaw = np.clip((YAW_LIMIT_DEG - abs(yaws[i])) / YAW_RAMP_DEG, 0.0, 1.0)
        gate[i] = g_blink * g_yaw
    du = np.clip(du, -MAX_DU, MAX_DU)
    dv = np.clip(dv, -MAX_DV, MAX_DV)
    return smooth(du), smooth(dv), smooth(gate)


def warp_eye(frame: np.ndarray, g: dict, shift: np.ndarray) -> None:
    """In place: displace the iris neighbourhood by `shift` px inside the eye
    opening, fading to zero toward the lids and corners."""
    import cv2

    if float(np.linalg.norm(shift)) < MIN_SHIFT_PX:
        return
    h, w = frame.shape[:2]
    m = 0.45 * g["w"]
    x0, y0 = np.floor(g["poly"].min(axis=0) - m).astype(int)
    x1, y1 = np.ceil(g["poly"].max(axis=0) + m).astype(int)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return
    roi = frame[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    mask = np.zeros((rh, rw), np.uint8)
    cv2.fillPoly(mask, [np.round(g["poly"] - [x0, y0]).astype(np.int32)], 255)
    k = max(1, int(round(g["w"] * 0.06)))
    mask = cv2.erode(mask, np.ones((k, k), np.uint8))
    blur = max(3, int(round(g["w"] * 0.12)) | 1)
    mask = cv2.GaussianBlur(mask, (blur, blur), 0).astype(np.float32) / 255.0
    gx, gy = np.meshgrid(np.arange(rw, dtype=np.float32), np.arange(rh, dtype=np.float32))
    target = g["iris"] - [x0, y0] + shift
    radius = 2.2 * g["r"] + float(np.linalg.norm(shift))
    r2 = ((gx - target[0]) ** 2 + (gy - target[1]) ** 2) / max(1e-6, radius * radius)
    weight = np.clip(1.0 - r2, 0.0, 1.0) ** 2 * mask
    map_x = (gx - float(shift[0]) * weight).astype(np.float32)
    map_y = (gy - float(shift[1]) * weight).astype(np.float32)
    frame[y0:y1, x0:x1] = cv2.remap(roi, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _detect(landmarker: Any, frame: np.ndarray, ts_ms: int | None) -> tuple[dict | None, float]:
    """Landmark one BGR frame (downscaled for speed; points mapped back)."""
    import cv2
    import mediapipe as mp

    h, w = frame.shape[:2]
    scale = min(1.0, LANDMARK_MAX_WIDTH / max(1, w))
    small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
    res = landmarker.detect_for_video(image, ts_ms) if ts_ms is not None else landmarker.detect(image)
    if not res.face_landmarks:
        return None, 90.0
    pts = np.array([[p.x * w, p.y * h] for p in res.face_landmarks[0]])
    yaw = yaw_degrees(res.facial_transformation_matrixes[0]) if res.facial_transformation_matrixes else 0.0
    return {e: eye_geometry(pts, e) for e in ("R", "L")}, yaw


def measure_reference(source: Path) -> dict[str, float] | None:
    """The speaker's at-camera iris position: medians over frames sampled at
    REF_SAMPLE_FPS across the whole source (talking heads look at the lens
    most of the time). None when too few faces are found."""
    import cv2

    landmarker = _landmarker(video=False)
    cap = cv2.VideoCapture(str(source))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, int(round(fps / REF_SAMPLE_FPS)))
        if total and total // step > REF_MAX_FRAMES:
            step = max(step, total // REF_MAX_FRAMES)
        rows: list[dict[str, float]] = []
        i = 0
        while True:
            if not cap.grab():
                break
            if i % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    g, _yaw = _detect(landmarker, frame, None)
                    if g is not None:
                        rows.append({f"{e}_{k}": g[e][k] for e in ("R", "L") for k in ("u", "v", "open")})
            i += 1
    finally:
        cap.release()
        landmarker.close()
    if len(rows) < REF_MIN_FACES:
        return None
    return {k: float(np.median([r[k] for r in rows])) for k in REF_KEYS}


def load_reference(asset_id: str, source: Path, working_dir: Path) -> dict[str, float] | None:
    """Cached `measure_reference` (working/{asset}/gaze_ref.json, keyed on
    version + source mtime)."""
    cache = working_dir / "gaze_ref.json"
    mtime = int(source.stat().st_mtime)
    try:
        data = json.loads(cache.read_text())
        if data.get("version") == EYE_CONTACT_VERSION and data.get("source_mtime") == mtime:
            return data.get("ref")
    except (OSError, ValueError):
        pass
    ref = measure_reference(source)
    try:
        working_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"version": EYE_CONTACT_VERSION, "source_mtime": mtime, "ref": ref}))
    except OSError:  # pragma: no cover
        log.warning("could not cache gaze reference for %s", asset_id)
    return ref


def correct_clip(clip: Path, ref: dict[str, float], config: ComposeConfig, log_file: Path | None = None) -> dict:
    """Correct one normalized clip in place. Two streaming passes (landmarks
    first, then warp + encode) so long shots never sit in memory. Returns
    stats; `applied` is False when nothing needed correcting."""
    import cv2

    landmarker = _landmarker(video=True)
    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or float(config.target_fps)
    geoms: list[dict | None] = []
    yaws: list[float] = []
    try:
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            g, yaw = _detect(landmarker, frame, int(i * 1000 / fps))
            geoms.append(g)
            yaws.append(yaw)
            i += 1
    finally:
        cap.release()
        landmarker.close()
    n = len(geoms)
    faces = sum(1 for g in geoms if g is not None)
    stats: dict[str, Any] = {"frames": n, "faces": faces, "applied": False, "mean_px": 0.0, "max_px": 0.0}
    if n == 0 or faces == 0:
        return stats
    du, dv, gate = gaze_plan(geoms, yaws, ref)
    shifts = []
    for i, g in enumerate(geoms):
        if g is None:
            shifts.append(None)
            continue
        shifts.append(
            {e: (du[i] * g[e]["ax"] + dv[i] * g[e]["perp"]) * g[e]["w"] * gate[i] for e in ("R", "L")}
        )
    mags = [max(float(np.linalg.norm(s[e])) for e in ("R", "L")) for s in shifts if s is not None]
    stats["mean_px"] = round(float(np.mean(mags)), 2)
    stats["max_px"] = round(float(np.max(mags)), 2)
    if stats["max_px"] < MIN_SHIFT_PX:
        return stats

    tmp = clip.with_name(clip.stem + ".eye.mp4")
    cap = cv2.VideoCapture(str(clip))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps}", "-i", "-",
        "-i", str(clip), "-map", "0:v", "-map", "1:a?",
        "-c:v", "libx264", "-preset", config.clip_preset, "-crf", str(config.clip_crf),
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        "-metadata", "creation_time=1970-01-01T00:00:00Z",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        str(tmp),
    ]
    if log_file is not None:
        try:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(" ".join(cmd) + "\n")
        except OSError:  # pragma: no cover
            pass
    # stderr goes to a temp file, not a pipe: an unread pipe can fill and
    # deadlock the frame writes. (Closing stdin then calling communicate()
    # raised "flush of closed file" in the first live run.)
    err_path = clip.with_name(clip.stem + ".eye.log")
    ok_encode = False
    with open(err_path, "w+b") as err_fh:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err_fh)
        try:
            i = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                s = shifts[i] if i < len(shifts) else None
                if s is not None:
                    for e in ("R", "L"):
                        warp_eye(frame, geoms[i][e], s[e])  # type: ignore[index]
                assert proc.stdin is not None
                proc.stdin.write(np.ascontiguousarray(frame).tobytes())
                i += 1
            ok_encode = True
        finally:
            cap.release()
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait()
        err_fh.seek(0)
        err = err_fh.read()
    err_path.unlink(missing_ok=True)
    if not ok_encode or proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"eye-contact encode failed: {err.decode(errors='replace')[-400:]}")
    os.replace(tmp, clip)
    stats["applied"] = True
    return stats


async def apply_eye_contact(
    clip: Path,
    asset_id: str,
    source: Path,
    config: ComposeConfig,
    log_file: Path | None = None,
) -> dict:
    """Best-effort async entry point used by clip extraction. Never raises:
    failures come back as {"applied": False, "error": ...} and the clip is
    left untouched."""
    data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    try:
        lock = _asset_locks.setdefault(asset_id, asyncio.Lock())
        async with lock:  # one reference measurement per asset
            ref = await asyncio.to_thread(load_reference, asset_id, source, data_dir / "working" / asset_id)
        if ref is None:
            return {"applied": False, "error": None, "reason": "no face found in the source"}
        stats = await asyncio.to_thread(correct_clip, clip, ref, config, log_file)
        stats["error"] = None
        return stats
    except EyeContactUnavailable as exc:
        log.warning("eye contact skipped: %s", exc)
        return {"applied": False, "error": str(exc)}
    except Exception as exc:
        log.warning("eye contact failed for %s; clip left uncorrected: %s", clip.name, exc, exc_info=True)
        return {"applied": False, "error": str(exc)}
