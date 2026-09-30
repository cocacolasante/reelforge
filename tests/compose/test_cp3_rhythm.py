"""Pro-editing CP3: visual rhythm through framing keys.

A continuous shot split into pieces used to look identical across its cuts
(the skate reel: 17 planned cuts, 2 visible), and a talking head held one
framing for a whole sentence. Framing keys change the picture WITHIN a shot
without changing its duration, so none of the xfade / caption / beat-sync
math moves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph_builder import build_final_command, framing_window
from reelforge_core.compose.styles import (
    HYPE_ALT_ZOOM,
    RHYTHM_FORCE_SEC,
    RHYTHM_MIN_GAP_SEC,
    phrase_boundaries,
    plan_edit,
    rhythm_keys,
)
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    TimelineShot,
)
from reelforge_core.qa.metrics import Shot, visible_changes
from tests.compose.test_graph_builder import _analysis as _graph_analysis
from tests.compose.test_jumpcuts import _speech_with_gaps
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _talk(duration: float, *, gap_every: int = 0, gap: float = 0.2):
    """Continuous 0.3s words 0.05s apart; every `gap_every`th word ends a
    phrase (a longer pause) when set."""
    words = []
    t, n = 0.1, 0
    while t + 0.3 <= duration - 0.1:
        n += 1
        words.append((round(t, 3), round(t + 0.3, 3), f"w{n}"))
        t += 0.3 + (gap if gap_every and n % gap_every == 0 else 0.05)
    return words


def _longest_static(keys, duration: float) -> float:
    times = [k[0] for k in keys] + [duration]
    return max(b - a for a, b in zip(times, times[1:]))


# ---- phrase boundaries -------------------------------------------------------


def test_phrase_boundaries_mark_sentence_ends_and_pauses_strong():
    words = [(0.0, 0.3, "Hi"), (0.35, 0.6, "there."), (0.65, 0.9, "So"), (1.2, 1.5, "yes")]
    got = phrase_boundaries(words)
    assert [strong for _, strong in got] == [False, True, True]
    assert got[0][0] == pytest.approx(0.325)  # midpoint of the gap, not a word


# ---- rhythm keys -------------------------------------------------------------


def test_rhythm_keys_change_about_every_three_seconds_at_phrases():
    words = _talk(15.0, gap_every=4)
    keys, nxt = rhythm_keys(15.0, words, 0)
    assert len(keys) >= 4
    assert _longest_static(keys, 15.0) <= RHYTHM_FORCE_SEC + 0.5
    # Every key after the first lands BETWEEN words, never inside one.
    for t, *_ in keys[1:]:
        assert not any(ws < t < we for ws, we, _ in words)
    # Keys are at least the minimum gap apart and clear of the shot's end.
    times = [k[0] for k in keys]
    assert all(b - a >= RHYTHM_MIN_GAP_SEC for a, b in zip(times, times[1:]))
    assert 15.0 - times[-1] >= RHYTHM_MIN_GAP_SEC
    assert nxt == len(keys)


def test_rhythm_keys_force_a_change_in_an_unbroken_sentence():
    # No pause anywhere: the old grammar held one framing for all 12s.
    words = _talk(12.0)
    keys, _ = rhythm_keys(12.0, words, 0)
    assert len(keys) >= 3
    assert _longest_static(keys, 12.0) <= 4.0


def test_rhythm_keys_move_through_silent_shots():
    keys, _ = rhythm_keys(8.0, [], 0)
    assert len(keys) >= 2
    assert _longest_static(keys, 8.0) <= 4.0


def test_rhythm_keys_cycle_continues_across_shots():
    first, nxt = rhythm_keys(2.0, [], 0)  # too short for an inner change
    second, _ = rhythm_keys(2.0, [], nxt)
    assert [k[1] for k in first] == [1.0]
    assert [k[1] for k in second] == [1.15]


def test_zooms_stay_within_the_timeline_validator():
    keys, _ = rhythm_keys(30.0, _talk(30.0, gap_every=3), 0)
    TimelineShot(
        kind="video", asset_id="a" * 64, in_ts=0.0, out_ts=30.0,
        framing_keys=[list(k) for k in keys],
    )


# ---- planners ----------------------------------------------------------------


def test_talking_head_keys_never_change_shot_durations():
    analysis = _analysis([_scene(0, 0, 14)], None).model_copy(
        update={"transcript": _speech_with_gaps()}
    )
    plan = plan_edit(
        "talking_head", [(0, 0.0, 14.0)], _reel([0], 0.0, 14.0), analysis,
        ComposeConfig(), None,
    )
    assert all(s.framing_keys for s in plan.shots)
    stripped = [s.duration for s in plan.shots]
    assert [s.out_ts - s.in_ts for s in plan.shots] == pytest.approx(stripped)


def test_hype_alternates_framing_on_pieces_of_one_shot():
    analysis = _analysis([_scene(0, 0, 20)], None)
    plan = plan_edit(
        "hype", [(0, 0.0, 20.0)], _reel([0], 0.0, 20.0), analysis,
        ComposeConfig(), BeatGrid(bpm=120.0, phase_sec=0.0),
    )
    assert len(plan.shots) > 2
    zooms = [s.framing_keys[0][1] for s in plan.shots if s.framing_keys]
    assert zooms[:4] == [1.0, HYPE_ALT_ZOOM, 1.0, HYPE_ALT_ZOOM]


# ---- render graph ------------------------------------------------------------


def _keyed_clip(keys) -> ClipInfo:
    return ClipInfo(
        path=Path("/tmp/clip_0000.mp4"), scene_index=0, in_ts=0.0, out_ts=10.0,
        duration=10.0, has_audio=True, effects_applied=[], framing_keys=keys,
        punch_in=1.25,
    )


def test_framing_window_is_even_and_inside_the_scaled_frame():
    for key in [(0, 1.0, 0.5, 0.5), (0, 1.3, 0.0, 0.0), (0, 1.15, 1.0, 1.0), (0, 1.3, 0.5, 0.42)]:
        sw, sh, x, y = framing_window(key, 1080, 1920)
        assert sw % 2 == 0 and sh % 2 == 0 and sw >= 1080 and sh >= 1920
        assert 0 <= x <= sw - 1080 and 0 <= y <= sh - 1920
    assert framing_window((0, 1.0, 0.5, 0.5), 1080, 1920) == (1080, 1920, 0, 0)
    assert framing_window((0, 1.3, 0.5, 0.5), 1080, 1920) == (1404, 2496, 162, 288)


def test_graph_retargets_a_scale_and_fixed_crop_one_sendcmd_per_key():
    keys = ((0.0, 1.0, 0.5, 0.42), (3.1, 1.15, 0.5, 0.42), (6.2, 1.3, 0.5, 0.42))
    plan = build_final_command(
        clips=[_keyed_clip(keys)],
        analysis=_graph_analysis(1),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(captions=CaptionStyle(mode="off"), effects=EffectsConfig(unsharp=False)),
        output_path=Path("/tmp/out.mp4"),
    )
    fc = plan.filter_complex
    assert fc.count("sendcmd=") == 2
    sw, sh, x, y = framing_window(keys[1], 1080, 1920)
    assert f"3.100 scale@fk0 w {sw}" in fc and f"crop@fc0 y {y}" in fc
    # The crop never changes size (growing one mid-stream hangs ffmpeg 5.1).
    assert "scale@fk0=w=1080:h=1920,crop@fc0=w=1080:h=1920:x=0:y=0,setsar=1" in fc
    assert "crop@fc0 w" not in fc and "crop@fc0 h" not in fc
    # Keys own the zoom: the clip's punch_in is not applied on top.
    assert "scale=1350" not in fc
    # The mezzanine length is exactly the clip's — keys never retime.
    assert plan.mezzanine_duration_sec == pytest.approx(10.0)


# ---- QA + validation -----------------------------------------------------------


def test_qa_counts_reframes_within_a_shot():
    shot = Shot(
        asset_id="a", in_ts=0.0, out_ts=10.0, duration=10.0,
        framing=((0.0, 1.0), (3.0, 1.15), (6.0, 1.15), (8.0, 1.3)),
    )
    changes, _ = visible_changes([shot])
    # 3.0 and 8.0 change the zoom; 6.0 repeats it and is not a change.
    assert [(c.t, c.why) for c in changes] == [(3.0, "reframe"), (8.0, "reframe")]


@pytest.mark.parametrize(
    "bad",
    [[[0.0, 1.0, 0.5]], [[-1.0, 1.0, 0.5, 0.5]], [[0.0, 2.0, 0.5, 0.5]], [[0.0, 1.2, 1.5, 0.5]]],
)
def test_timeline_shot_rejects_bad_framing_keys(bad):
    with pytest.raises(ValidationError):
        TimelineShot(kind="video", asset_id="a" * 64, in_ts=0.0, out_ts=5.0, framing_keys=bad)


def test_director_punch_does_not_override_framing_keys():
    from reelforge_core.compose.director import apply_director
    from reelforge_core.compose.styles import EditPlan, PlannedShot

    keys = ((0.0, 1.2, 0.5, 0.5),)
    plan = EditPlan(
        style="hype",
        shots=[PlannedShot(0, 0.0, 3.0, framing_keys=keys), PlannedShot(0, 3.0, 6.0)],
        per_cut=[("cut", 0.04)],
    )
    raw = {
        "shots": [
            {"index": 0, "punch_in": 1.3, "punch_in_animated": True, "reason": "x"},
            {"index": 1, "punch_in": 1.3, "reason": "y"},
        ],
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, _ = apply_director(plan, raw, _analysis([_scene(0, 0, 60)], None))
    keyed, plain = new_plan.shots
    assert keyed.framing_keys == keys and keyed.punch_in is None and not keyed.punch_in_animated
    assert plain.punch_in == 1.3


def test_real_render_through_tight_to_wide_keys_does_not_hang(tmp_path):
    """The render that hung live: keys that zoom back OUT, feeding an xfade.
    Runs real ffmpeg with a timeout; the output must be the planned length."""
    import subprocess

    def clip(i: int, keys) -> ClipInfo:
        path = tmp_path / f"c{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=3",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
            check=True, capture_output=True,
        )
        return ClipInfo(path=path, scene_index=i, in_ts=0.0, out_ts=3.0, duration=3.0,
                        has_audio=True, effects_applied=[], framing_keys=keys)

    keys = ((0.0, 1.3, 0.5, 0.42), (1.0, 1.0, 0.5, 0.42), (2.0, 1.15, 0.5, 0.42))
    out = tmp_path / "out.mp4"
    plan = build_final_command(
        clips=[clip(0, keys), clip(1, keys)],
        analysis=_graph_analysis(2),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(captions=CaptionStyle(mode="off"), effects=EffectsConfig(unsharp=False),
                             transition={"kind": "fade", "duration_sec": 0.3}),
        output_path=out,
        final_pass=False,
    )
    subprocess.run(plan.args, check=True, capture_output=True, timeout=120)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=width,height:format=duration", "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert probe[0] == "1080,1920"
    assert float(probe[1]) == pytest.approx(plan.mezzanine_duration_sec, abs=0.1)


def test_hype_piece_after_the_slowmo_punch_opens_wide():
    from reelforge_core.compose.styles import hype_alt_zoom

    # The live skate reel: slow-mo money shot punched to 1.2, then the next
    # beat piece of the same shot ALSO at 1.2 — an invisible cut.
    assert hype_alt_zoom(1.2) == 1.0
    assert hype_alt_zoom(1.0) == HYPE_ALT_ZOOM
