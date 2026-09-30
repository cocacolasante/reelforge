"""Pro-editing CP5: per-frame subject tracking for the reframe crop.

The pure pieces (speaker choice, the camera spring, path thinning, the
ffmpeg expression) are tested directly; decoding, the motion fallback and
the rendered crop run real ffmpeg on synthetic footage."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.compose.clips import _pan_key, build_clip_command
from reelforge_core.compose.facetrack import (
    DEAD_ZONE,
    KEY_EVERY_SEC,
    Face,
    Track,
    _read_ppm,
    aim_framing_keys,
    camera_path,
    crop_fraction,
    decode_frames,
    faces_from_landmarks,
    pick_subject,
    piecewise_expr,
    speech_intervals,
    thin_path,
    track_subject,
)
from reelforge_core.models import ComposeConfig, Transcript, TranscriptSegment, TranscriptWord

CROP_W = 0.316  # a 9:16 crop out of 16:9


# ---- pure: observation -------------------------------------------------------


def test_read_ppm_handles_comments_and_eof():
    data = b"P6\n# made by a test\n2 1\n255\n" + bytes([255, 0, 0, 0, 255, 0])
    stream = io.BytesIO(data)
    assert _read_ppm(stream) == (2, 1, bytes([255, 0, 0, 0, 255, 0]))
    assert _read_ppm(stream) is None


def test_faces_from_landmarks():
    pts = [SimpleNamespace(x=0.0, y=0.0) for _ in range(478)]
    pts[33], pts[263] = SimpleNamespace(x=0.40, y=0.30), SimpleNamespace(x=0.50, y=0.32)
    pts[13], pts[14] = SimpleNamespace(x=0.45, y=0.50), SimpleNamespace(x=0.45, y=0.53)
    pts[10], pts[152] = SimpleNamespace(x=0.45, y=0.20), SimpleNamespace(x=0.45, y=0.60)
    (face,) = faces_from_landmarks([pts])
    assert face.x == pytest.approx(0.45) and face.y == pytest.approx(0.31)
    assert face.mouth == pytest.approx(0.03 / 0.40)
    assert faces_from_landmarks([[SimpleNamespace(x=0, y=0)]]) == []  # malformed: skipped


def _two_faces(t: float, left_mouth: float, right_mouth: float) -> tuple[float, list[Face]]:
    return (t, [Face(0.75, 0.3, right_mouth), Face(0.25, 0.3, left_mouth)])


def test_active_speaker_wins_with_hysteresis():
    frames = []
    for i in range(36):  # 6s at 6 fps
        t = i / 6.0
        # Left talks for 3s, then right talks.
        wobble = 0.08 if i % 2 else 0.0
        frames.append(_two_faces(t, wobble if t < 3 else 0.02, wobble if t >= 3 else 0.02))
    picked, switches = pick_subject(frames, speech=[(0.0, 6.0)])
    xs = [f.x for _, f in picked]
    assert xs[5] == 0.25 and xs[-1] == 0.75
    assert switches == 1


def test_no_switch_while_nobody_is_talking():
    frames = [_two_faces(i / 6.0, 0.02, 0.08 if i % 2 else 0.0) for i in range(36)]
    frames[:3] = [_two_faces(i / 6.0, 0.08 if i % 2 else 0.0, 0.0) for i in range(3)]
    picked, switches = pick_subject(frames, speech=[])
    assert switches == 0 and {f.x for _, f in picked[3:]} == {0.25}


def test_single_face_and_empty_frames():
    picked, switches = pick_subject([(0.0, [Face(0.6, 0.3, 0.0)]), (0.2, [])])
    assert picked[0][1].x == 0.6 and picked[1][1] is None and switches == 0


# ---- pure: the camera ------------------------------------------------------------


def test_camera_holds_inside_the_dead_zone():
    wiggle = DEAD_ZONE * CROP_W / 2.0 * 0.8
    targets = [(i / 6.0, 0.5 + (wiggle if i % 2 else 0.0)) for i in range(60)]
    path = camera_path(targets, CROP_W, 3.0)
    xs = [x for _, x in path]
    assert max(xs) - min(xs) < 1e-6


def test_camera_eases_to_a_moved_subject_without_overshooting():
    targets = [(i / 6.0, 0.35 if i < 6 else 0.65) for i in range(60)]
    path = camera_path(targets, CROP_W, 3.0)
    xs = [x for _, x in path]
    assert xs[6] < 0.45  # eases off, no jump the frame the subject moves
    assert xs[-1] == pytest.approx(0.65, abs=0.01)  # settled within ~9s
    assert max(xs) <= 0.65 + 1e-6  # critically damped: never past the target
    assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))


def test_camera_cuts_at_a_speaker_switch_and_holds_lost_targets():
    targets = [(i / 6.0, 0.3 if i < 12 else 0.7) for i in range(24)]
    cut = camera_path(targets, CROP_W, 3.0, cuts={12 / 6.0})
    assert cut[12][1] == pytest.approx(0.7)
    lost = camera_path([(0.0, 0.3), (0.5, None), (3.0, None)], CROP_W, 3.0)
    assert [round(x, 3) for _, x in lost] == [0.3, 0.3, 0.3]


def test_camera_keeps_the_crop_in_frame():
    path = camera_path([(i / 6.0, 0.99) for i in range(40)], CROP_W, 3.0)
    assert max(x for _, x in path) <= 1.0 - CROP_W / 2.0 + 1e-9
    assert camera_path([(0.0, None)], CROP_W, 3.0) == [(0.0, 0.5)]


def test_thin_path_spacing_simplification_and_ends():
    moving = [(i / 6.0, 0.3 + 0.02 * i) for i in range(31)]  # a straight pan
    keys = thin_path(moving, 5.0)
    assert keys[0][0] == 0.0 and keys[-1][0] == 5.0
    assert len(keys) == 2  # a straight line needs no interior keys
    curve = [(i / 6.0, 0.5 + (0.1 if i > 12 else 0.0)) for i in range(60)]
    keys = thin_path(curve, 10.0)
    assert all(b[0] - a[0] >= KEY_EVERY_SEC - 1e-6 for a, b in zip(keys, keys[1:-1]))
    assert len(thin_path([(i / 6.0, 0.5) for i in range(6000)], 1000.0)) == 2


def test_piecewise_expr_matches_linear_interpolation():
    path = [(0.0, 0.3), (2.0, 0.5), (4.0, 0.5), (5.0, 0.7)]
    expr = piecewise_expr(path, "T").replace("\\,", ",")

    def evaluate(t: float) -> float:
        env = {"lt": lambda a, b: a < b, "T": t}
        py = expr.replace("if(", "_if(")
        return eval(py, {"_if": lambda c, a, b: a if c else b, **env})  # noqa: S307

    assert evaluate(-1.0) == pytest.approx(0.3)
    assert evaluate(1.0) == pytest.approx(0.4)
    assert evaluate(3.0) == pytest.approx(0.5)
    assert evaluate(4.5) == pytest.approx(0.6)
    assert evaluate(9.0) == pytest.approx(0.7)


def test_aim_framing_keys_puts_the_eyes_a_third_down():
    track = Track(path=[(0.0, 0.5)], source="face", eye_y=0.30, face_x=0.62)
    keys = ((0.0, 1.0, 0.5, 0.42), (3.0, 1.3, 0.5, 0.42))
    cropped = aim_framing_keys(keys, track, cropped=True)
    assert [k[0:2] for k in cropped] == [(0.0, 1.0), (3.0, 1.3)]
    assert cropped[1][3] == pytest.approx(0.30 + (1 / 6) / 1.3, abs=1e-4)
    assert cropped[1][2] == 0.5  # the crop already follows the face
    assert aim_framing_keys(keys, track, cropped=False)[1][2] == 0.62
    assert aim_framing_keys(keys, None, True) == keys


def test_crop_fraction_and_speech_intervals():
    assert crop_fraction(1920, 1080, 1080, 1920) == pytest.approx(0.3164, abs=1e-3)
    assert crop_fraction(1080, 1920, 1080, 1920) == 1.0
    words = [TranscriptWord(start=s, end=s + 0.3, word="w", probability=1.0) for s in (1.0, 1.5, 3.0)]
    t = Transcript(language="en", language_probability=1.0, duration=5.0,
                   segments=[TranscriptSegment(start=1.0, end=3.3, text="w w w", words=words)])
    assert speech_intervals(t, 0.0, 5.0) == [(1.0, 1.8), (3.0, 3.3)]
    assert speech_intervals(None, 0.0, 5.0) == []


def test_pan_key_names_the_track():
    track = Track(path=[(0.0, 0.4), (2.0, 0.6)], source="face")
    assert _pan_key(None, track.path, track, 1.0).startswith(f"track:{track.digest}|f1|")
    assert _pan_key((0.4, 0.6), None, None, 1.0) == "0.4000-0.6000"
    assert _pan_key(None, None, None, 1.0) == "none"


# ---- real ffmpeg ---------------------------------------------------------------------


def _moving_box_clip(path: Path, seconds: float = 4.0) -> Path:
    """1920x1080 grey clip with a bright box sweeping left -> right."""
    subprocess.run(
        # overlay, not drawbox: drawbox doesn't evaluate `t` per frame here.
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:size=1920x1080:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"color=c=white:size=200x200:rate=30:duration={seconds}",
         "-filter_complex", f"[0:v][1:v]overlay=x='200+1400*t/{seconds}':y=400",
         "-c:v", "libx264", "-preset", "ultrafast", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_decode_streams_small_frames(tmp_path):
    src = _moving_box_clip(tmp_path / "box.mp4")
    frames = list(decode_frames(src, 1.0, 3.0))
    assert 11 <= len(frames) <= 13  # 2s at 6 fps
    t, rgb = frames[0]
    assert t == 0.0 and rgb.shape == (270, 480, 3)


def test_motion_fallback_follows_the_box_and_caches(tmp_path, monkeypatch):
    from reelforge_core.compose import facetrack

    monkeypatch.setattr(facetrack, "_landmarker", lambda: None)  # no faces here anyway
    src = _moving_box_clip(tmp_path / "box.mp4")
    working = tmp_path / "working"
    (working / "asset").mkdir(parents=True)
    track = track_subject(src, 0.0, 4.0, (1080, 1920), asset_id="asset", working_root=working)
    assert track is not None and track.source == "motion" and track.coverage is None
    xs = [x for _, x in track.path]
    assert xs[-1] > xs[0] + 0.2  # the camera went where the box went
    cached = list((working / "asset" / "tracks").glob("*.json"))
    assert len(cached) == 1 and json.loads(cached[0].read_text())["source"] == "motion"
    monkeypatch.setattr(facetrack, "_track", lambda *a, **k: pytest.fail("cache miss"))
    again = track_subject(src, 0.0, 4.0, (1080, 1920), asset_id="asset", working_root=working)
    assert again.path == track.path


def test_tracking_failure_is_none(tmp_path):
    assert track_subject(tmp_path / "missing.mp4", 0.0, 2.0, (1080, 1920)) is None


def test_tracked_crop_renders(tmp_path):
    src = _moving_box_clip(tmp_path / "box.mp4")
    out = tmp_path / "clip.mp4"
    cmd = build_clip_command(
        source=src, out_path=out, in_ts=1.0, out_ts=3.0, config=ComposeConfig(aspect="9:16"),
        has_audio=False, is_hdr=False,
        pan_path=[(0.0, 0.3), (1.0, 0.5), (2.0, 0.7)],
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop=w=floor(ih*1080/1920/2)*2:h=ih" in vf and "if(lt((t-1.000)" in vf
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
         "-of", "csv=p=0", str(out)], check=True, capture_output=True, text=True,
    ).stdout.split()
    assert probe[0] == "1080,1920" and float(probe[1]) == pytest.approx(2.0, abs=0.1)


def test_full_width_crop_skips_tracking(tmp_path):
    src = tmp_path / "portrait.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=gray:size=1080x1920:rate=30:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True, capture_output=True,
    )
    track = track_subject(src, 0.0, 3.0, (1080, 1920))
    assert track.source == "full" and track.path == [(0.0, 0.5), (3.0, 0.5)]
