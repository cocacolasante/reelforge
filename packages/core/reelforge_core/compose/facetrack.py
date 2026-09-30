"""Per-frame subject tracking for the reframe crop (pro-editing CP5).

The old reframe (compose/reframe.py) sampled 12 frames and panned linearly
between two guesses — on real footage it landed at 0.47-0.56 of the frame,
a centre crop in all but name. This follows the subject through the shot:

1. Decode the clip's source range through an ffmpeg pipe at SAMPLE_FPS and
   DECODE_WIDTH as PPM (the header carries the real size, rotation
   included) — ONE frame in memory at a time, so a 4K clip costs what a
   480px one does.
2. MediaPipe FaceLandmarker (VIDEO mode, up to 2 faces) gives each face's
   eye line and mouth opening. With two faces the ACTIVE speaker wins: the
   one whose mouth moves while the transcript says someone is talking, with
   hysteresis so the crop doesn't ping-pong.
3. No usable face (skate, surf, the board being waxed): the motion centroid
   of consecutive frames, smoothed harder.
4. A camera operator's smoothing: the crop holds while the subject stays in
   the central DEAD_ZONE of the frame, then a critically damped spring
   eases it over; a speaker switch is a cut, not a whip-pan. The path is
   thinned to at most one key per KEY_EVERY_SEC.

The result — a piecewise-linear x path in clip-relative source seconds plus
the eye line — is cached per asset at `working/{asset}/tracks/`, keyed on
FACETRACK_VERSION, the range, the source mtime and the target aspect. Any
failure returns None and the caller falls back to reframe.estimate_pan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

FACETRACK_VERSION = "f1"
SAMPLE_FPS = 6.0
DECODE_WIDTH = 480
MAX_FACES = 2
MIN_FACE_SHARE = 0.25  # of frames with a face, or the motion fallback runs
DEAD_ZONE = 0.20  # of the crop width: the subject may wander this much
SPRING_OMEGA_FACE = 3.0  # rad/s — settles in ~1.5s
SPRING_OMEGA_MOTION = 1.2  # motion centroids are noisy: ease harder
KEY_EVERY_SEC = 0.5
MAX_KEYS = 120
SIMPLIFY_TOL = 0.004  # of frame width; drop keys a straight line explains
HOLD_SEC = 1.0  # a lost face holds its place this long
SWITCH_MIN_SEC = 1.5  # speaker switches at most this often
SWITCH_RATIO = 1.6  # the other mouth must move this much more
EYE_LINE = 1.0 / 3.0  # eyes a third of the way down a zoomed frame

# FaceMesh indices: outer eye corners, inner lips, forehead + chin.
_EYE_L, _EYE_R, _LIP_TOP, _LIP_BOT, _BROW, _CHIN = 33, 263, 13, 14, 10, 152


@dataclass
class Face:
    x: float  # eye-line centre, fraction of frame width
    y: float  # eye line, fraction of frame height
    mouth: float  # lip gap / face height


@dataclass
class Track:
    path: list[tuple[float, float]]  # (seconds from clip start, crop centre x)
    source: str  # face | motion
    eye_y: float | None = None  # median eye line, fraction of frame height
    face_x: float | None = None  # median subject x (for uncropped framing keys)
    coverage: float | None = None  # face-bearing frames with the face in the crop
    switches: int = 0
    version: str = FACETRACK_VERSION
    extra: dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        blob = json.dumps([self.version, [[round(t, 3), round(x, 4)] for t, x in self.path]])
        return hashlib.sha1(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# decode (streamed)
# --------------------------------------------------------------------------


def _read_ppm(stream) -> "tuple[int, int, bytes] | None":
    """One binary PPM frame off the pipe, or None at EOF."""
    def token() -> bytes:
        out = b""
        while True:
            ch = stream.read(1)
            if not ch:
                return out
            if ch == b"#":
                stream.readline()
                continue
            if ch.isspace():
                if out:
                    return out
                continue
            out += ch

    magic = token()
    if magic != b"P6":
        return None
    w, h, _maxval = int(token()), int(token()), int(token())
    data = stream.read(w * h * 3)
    if len(data) < w * h * 3:
        return None
    return w, h, data


def decode_frames(source: Path, in_ts: float, out_ts: float, fps: float = SAMPLE_FPS):
    """Yield (seconds from in_ts, HxWx3 RGB uint8) at `fps`, one at a time."""
    import numpy as np

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, in_ts):.3f}", "-i", str(source),
        "-t", f"{max(0.05, out_ts - in_ts):.3f}", "-an",
        "-vf", f"fps={fps},scale={DECODE_WIDTH}:-2",
        "-f", "image2pipe", "-vcodec", "ppm", "-",
    ]
    # Buffered: a raw pipe's read(n) returns short reads, which would look
    # like a truncated frame.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 20)
    try:
        i = 0
        while True:
            frame = _read_ppm(proc.stdout)
            if frame is None:
                break
            w, h, data = frame
            yield i / fps, np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------


def faces_from_landmarks(face_landmarks) -> list[Face]:
    """MediaPipe landmark lists (normalised coords) -> Faces."""
    out = []
    for lm in face_landmarks or []:
        try:
            el, er = lm[_EYE_L], lm[_EYE_R]
            height = max(1e-3, abs(lm[_CHIN].y - lm[_BROW].y))
            out.append(
                Face(
                    x=(el.x + er.x) / 2.0,
                    y=(el.y + er.y) / 2.0,
                    mouth=abs(lm[_LIP_BOT].y - lm[_LIP_TOP].y) / height,
                )
            )
        except (IndexError, AttributeError):
            continue
    return out


def _speaking(t: float, speech: list[tuple[float, float]]) -> bool:
    return any(a - 0.1 <= t <= b + 0.1 for a, b in speech)


def pick_subject(
    frames: list[tuple[float, list[Face]]],
    speech: list[tuple[float, float]] | None = None,
) -> tuple[list[tuple[float, Face | None]], int]:
    """One face per frame (or None) and the number of speaker switches.

    Faces are held in left/right slots (sorted by x) so two people keep
    their identity; the active slot changes only when the other mouth has
    moved SWITCH_RATIO x more over the last second WHILE someone is talking,
    and never within SWITCH_MIN_SEC of the last switch. Pure."""
    speech = speech or []
    motion: dict[int, list[tuple[float, float]]] = {0: [], 1: []}  # slot -> (t, |d mouth|)
    last_mouth: dict[int, float] = {}
    active: int | None = None
    last_switch = -1e9
    switches = 0
    out: list[tuple[float, Face | None]] = []
    for t, faces in frames:
        if not faces:
            out.append((t, None))
            continue
        slots = sorted(faces, key=lambda f: f.x)[:MAX_FACES]
        if len(slots) == 1:
            # One face: it keeps whichever slot it is nearest to.
            only = slots[0]
            if active is None:
                active = 0
            out.append((t, only))
            last_mouth = {active: only.mouth}
            continue
        for s, f in enumerate(slots):
            if s in last_mouth:
                motion[s].append((t, abs(f.mouth - last_mouth[s])))
            last_mouth[s] = f.mouth
            motion[s] = [(mt, m) for mt, m in motion[s] if t - mt <= 1.0]
        energy = {s: sum(m for _, m in motion[s]) for s in (0, 1)}
        if active is None:
            active = max((0, 1), key=lambda s: energy[s])
        other = 1 - active
        if (
            _speaking(t, speech)
            and t - last_switch >= SWITCH_MIN_SEC
            and energy[other] > SWITCH_RATIO * max(energy[active], 1e-4)
        ):
            active, last_switch = other, t
            switches += 1
        out.append((t, slots[min(active, len(slots) - 1)]))
    return out, switches


# --------------------------------------------------------------------------
# smoothing (pure)
# --------------------------------------------------------------------------


def camera_path(
    targets: list[tuple[float, float | None]],
    crop_w: float,
    omega: float,
    cuts: set[float] | None = None,
) -> list[tuple[float, float]]:
    """A camera operator's x path over (t, subject x | None) samples.

    Holds while the subject is inside the central DEAD_ZONE of the crop,
    otherwise eases toward it on a critically damped spring; at a time in
    `cuts` (a speaker switch) it jumps. Lost targets hold for HOLD_SEC, then
    the camera stays where it is. Clamped so the crop stays in frame."""
    lo, hi = crop_w / 2.0, 1.0 - crop_w / 2.0
    clamp = (lambda v: min(max(v, lo), hi)) if lo < hi else (lambda v: 0.5)
    known = [x for _, x in targets if x is not None]
    if not known:
        return [(t, 0.5) for t, _ in targets]
    pos = clamp(known[0])
    vel = 0.0
    goal = pos
    last_seen, last_x = -1e9, known[0]
    out: list[tuple[float, float]] = []
    prev_t = targets[0][0] if targets else 0.0
    for t, x in targets:
        dt = max(1e-3, t - prev_t)
        prev_t = t
        if x is not None:
            last_seen, last_x = t, x
        elif t - last_seen <= HOLD_SEC:
            x = last_x
        if x is not None:
            if cuts and any(abs(t - c) < 1e-6 for c in cuts):
                pos = goal = clamp(x)
                vel = 0.0
            elif abs(x - goal) > DEAD_ZONE * crop_w / 2.0:
                goal = clamp(x)
        acc = omega * omega * (goal - pos) - 2.0 * omega * vel
        vel += acc * dt
        pos = clamp(pos + vel * dt)
        out.append((t, pos))
    return out


def thin_path(path: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """At most one key per KEY_EVERY_SEC (fewer on long clips, MAX_KEYS),
    then drop every key a straight line between its neighbours explains."""
    if not path:
        return [(0.0, 0.5), (duration, 0.5)]
    step = max(KEY_EVERY_SEC, duration / MAX_KEYS)
    keys: list[tuple[float, float]] = []
    for t, x in path:
        if not keys or t - keys[-1][0] >= step - 1e-6:
            keys.append((round(t, 3), round(x, 4)))
    if keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    if duration - keys[-1][0] > 1e-3:
        keys.append((round(duration, 3), round(path[-1][1], 4)))
    out = [keys[0]]
    for i in range(1, len(keys) - 1):
        (t0, x0), (t1, x1), (t2, x2) = out[-1], keys[i], keys[i + 1]
        guess = x0 + (x2 - x0) * (t1 - t0) / max(1e-6, t2 - t0)
        if abs(guess - x1) > SIMPLIFY_TOL:
            out.append(keys[i])
    out.append(keys[-1])
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[len(s) // 2]


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def crop_fraction(frame_w: int, frame_h: int, target_w: int, target_h: int) -> float:
    """Width of a full-height target-aspect crop, as a fraction of the frame."""
    if frame_w <= 0 or frame_h <= 0:
        return 1.0
    return min(1.0, (frame_h * target_w / target_h) / frame_w)


def _cache_path(working_root: Path, asset_id: str, key: dict) -> Path:
    digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:20]
    return working_root / asset_id / "tracks" / f"{digest}.json"


def track_subject(
    source: Path,
    in_ts: float,
    out_ts: float,
    target: tuple[int, int],
    *,
    speech: list[tuple[float, float]] | None = None,
    asset_id: str | None = None,
    working_root: Path | None = None,
    aim: bool = False,
) -> Track | None:
    """The subject track for one clip, cached; None on any failure. `aim`:
    the clip has framing keys to point at a face, so track even when the
    crop is the whole frame width (a portrait source)."""
    key = {
        "v": FACETRACK_VERSION,
        "in": round(in_ts, 3),
        "out": round(out_ts, 3),
        "aspect": f"{target[0]}x{target[1]}",
        "aim": aim,
    }
    cache = None
    try:
        key["mtime"] = int(source.stat().st_mtime)
        if asset_id and working_root is not None and (working_root / asset_id).is_dir():
            cache = _cache_path(working_root, asset_id, key)
            if cache.exists():
                data = json.loads(cache.read_text())
                data["path"] = [tuple(p) for p in data["path"]]
                return Track(**data)
    except (OSError, ValueError, TypeError):
        cache = None
    try:
        track = _track(source, in_ts, out_ts, target, speech or [], aim)
    except Exception:  # noqa: BLE001 — never fails a render
        log.warning("face tracking failed for %s [%.2f-%.2f]", source, in_ts, out_ts, exc_info=True)
        return None
    if track is not None and cache is not None:
        try:
            from reelforge_core.io_utils import write_json_atomic

            cache.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(cache, asdict(track))
        except OSError:  # pragma: no cover
            pass
    return track


def _landmarker():
    """A VIDEO-mode landmarker for up to MAX_FACES, or None without mediapipe."""
    try:
        from reelforge_core.compose.eyecontact import _landmarker as make

        return make(True, num_faces=MAX_FACES)
    except Exception as exc:  # noqa: BLE001
        log.info("face tracking without faces (%s)", exc)
        return None


def _track(
    source: Path,
    in_ts: float,
    out_ts: float,
    target: tuple[int, int],
    speech: list[tuple[float, float]],
    aim: bool = False,
) -> Track | None:
    import numpy as np

    from reelforge_core.vision import frame_diff_profile

    duration = max(0.05, out_ts - in_ts)
    rel_speech = [(a - in_ts, b - in_ts) for a, b in speech if b >= in_ts and a <= out_ts]
    landmarker = _landmarker()
    observed: list[tuple[float, list[Face]]] = []
    motion: list[tuple[float, float | None]] = []
    crop_w = 1.0
    prev_grey = None
    try:
        for t, rgb in decode_frames(source, in_ts, out_ts):
            h, w = rgb.shape[:2]
            crop_w = crop_fraction(w, h, target[0], target[1])
            if crop_w >= 0.999 and not aim:
                # The crop is the whole frame (phone footage whose probe
                # reports pre-rotation landscape dims): nothing to follow.
                return Track(path=[(0.0, 0.5), (round(duration, 3), 0.5)], source="full")
            faces: list[Face] = []
            if landmarker is not None:
                import mediapipe as mp

                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
                result = landmarker.detect_for_video(image, int(round(t * 1000)))
                faces = faces_from_landmarks(result.face_landmarks)
            observed.append((t, faces))
            grey = rgb.mean(axis=2).astype(np.uint8)
            if prev_grey is not None and prev_grey.shape == grey.shape:
                col, _ = frame_diff_profile(prev_grey, grey)
                total = float(col.sum())
                motion.append((t, float((col * np.arange(col.size)).sum() / total / col.size)
                               if total > 1e-3 else None))
            else:
                motion.append((t, None))
            prev_grey = grey
    finally:
        if landmarker is not None:
            landmarker.close()
    if not observed:
        return None

    with_face = sum(1 for _, f in observed if f)
    if with_face / len(observed) >= MIN_FACE_SHARE:
        picked, switches = pick_subject(observed, rel_speech)
        cuts: set[float] = set()
        prev = None
        for t, f in picked:
            if f is not None and prev is not None and abs(f.x - prev.x) > crop_w / 2.0:
                cuts.add(t)  # a speaker switch (or a jump): cut, don't whip
            if f is not None:
                prev = f
        raw = camera_path([(t, f.x if f else None) for t, f in picked], crop_w, SPRING_OMEGA_FACE, cuts)
        seen = [(pos, f) for (_, pos), (_, f) in zip(raw, picked) if f is not None]
        inside = sum(1 for pos, f in seen if abs(f.x - pos) <= crop_w / 2.0 * 0.9)
        return Track(
            path=thin_path(raw, duration),
            source="face",
            eye_y=_median([f.y for _, f in seen]),
            face_x=_median([f.x for _, f in seen]),
            coverage=round(inside / len(seen), 3) if seen else None,
            switches=switches,
        )
    raw = camera_path(motion, crop_w, SPRING_OMEGA_MOTION)
    return Track(path=thin_path(raw, duration), source="motion")


# --------------------------------------------------------------------------
# framing keys follow the face (graph-side, so nothing re-extracts)
# --------------------------------------------------------------------------


def aim_framing_keys(keys: tuple, track: Track | None, cropped: bool) -> tuple:
    """Point zoomed framing keys at the tracked face: eyes a third of the
    way down the zoomed frame, horizontally on the face (a cropped clip
    already follows it, so its keys stay centred). Zoom and times unchanged."""
    if not keys or track is None or track.eye_y is None:
        return keys
    out = []
    for t, zoom, cx, cy in keys:
        z = max(1.0, float(zoom))
        new_cy = min(1.0, max(0.0, track.eye_y + (0.5 - EYE_LINE) / z))
        new_cx = cx if cropped or track.face_x is None else min(1.0, max(0.0, track.face_x))
        out.append((t, zoom, round(new_cx, 4), round(new_cy, 4)))
    return tuple(out)


def piecewise_expr(path: list[tuple[float, float]], var: str) -> str:
    """An ffmpeg expression for x(var) through the path's keys, linear
    between them and flat past the ends. Commas escaped for -vf use."""
    if not path:
        return "0.5"
    if len(path) == 1:
        return f"{path[0][1]:.4f}"
    expr = f"{path[-1][1]:.4f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(path, path[1:]))):
        span = max(1e-3, t1 - t0)
        seg = f"({x0:.4f}+({x1 - x0:.4f})*({var}-{t0:.3f})/{span:.3f})"
        expr = f"if(lt({var}\\,{t1:.3f})\\,{seg}\\,{expr})"
    return f"if(lt({var}\\,{path[0][0]:.3f})\\,{path[0][1]:.4f}\\,{expr})"


def speech_intervals(transcript, in_ts: float, out_ts: float) -> list[tuple[float, float]]:
    """Source-time word spans inside the clip, merged across short gaps."""
    if transcript is None:
        return []
    spans: list[tuple[float, float]] = []
    for seg in transcript.segments:
        if seg.end < in_ts or seg.start > out_ts:
            continue
        for w in seg.words:
            if w.end < in_ts or w.start > out_ts:
                continue
            if spans and w.start - spans[-1][1] < 0.4:
                spans[-1] = (spans[-1][0], max(spans[-1][1], w.end))
            else:
                spans.append((w.start, w.end))
    return spans

