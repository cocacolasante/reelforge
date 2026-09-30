"""Pro-editing CP8: action cutting — events kept whole, cut-ins at the
motion low, a speed ramp into the impact, visible cuts, beat snapping that
never lands inside an event."""

from __future__ import annotations

import random

import pytest

from reelforge_core.compose.action import (
    FILL_MIN_SEC,
    PAYOFF_HOLD_SEC,
    Piece,
    action_pieces,
    cut_in,
    event_cold_open,
    money_event,
    snap_cuts,
)
from reelforge_core.compose.beats import BeatGrid
from reelforge_core.compose.styles import plan_edit
from reelforge_core.models import ComposeConfig, EnergyPoint
from reelforge_core.reels.events import ActionEvent
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _ev(s, e, peak=None, strength=5.0) -> ActionEvent:
    return ActionEvent(s, e, peak if peak is not None else (s + e) / 2, strength)


def _flat(t: float) -> float:
    return 0.0


def _contiguous(pieces, s, e):
    assert pieces[0].start == pytest.approx(s, abs=1e-3)
    assert pieces[-1].end == pytest.approx(e, abs=1e-3)
    for a, b in zip(pieces, pieces[1:]):
        assert a.end == pytest.approx(b.start, abs=1e-3)


def _no_cut_inside(pieces, events):
    for a in pieces[:-1]:
        for ev in events:
            assert not ev.start_sec + 1e-3 < a.end < ev.end_sec - 1e-3, (a.end, ev)


def test_events_are_never_cut_and_every_piece_changes_framing():
    events = [_ev(5.0, 7.0), _ev(12.0, 18.0), _ev(22.0, 23.0)]
    pieces, _ = action_pieces(0.0, 30.0, events, _flat, grid=BeatGrid(120.0, 0.0))
    _contiguous(pieces, 0.0, 30.0)
    _no_cut_inside(pieces, events)
    # Every cut changes framing: a shot's LAST framing vs the next one's first.
    assert all(a.keys[-1][1] != b.keys[0][1] for a, b in zip(pieces, pieces[1:]))
    # The 6s event is ONE shot that reframes inside instead of cutting.
    long_ev = next(p for p in pieces if p.start <= 12.0 and p.end >= 18.0)
    assert len(long_ev.keys) >= 3


def test_random_spans_keep_every_invariant():
    rng = random.Random(7)
    for _ in range(200):
        n = rng.randint(0, 4)
        events, t = [], 1.0
        for _ in range(n):
            s = t + rng.uniform(0.5, 6.0)
            e = s + rng.uniform(0.5, 5.0)
            events.append(_ev(round(s, 2), round(e, 2), strength=rng.uniform(3, 8)))
            t = e
        end = t + rng.uniform(0.0, 8.0)
        money = money_event(events, [(0.0, end)])
        pieces, _ = action_pieces(0.0, end, events, lambda x: rng.uniform(-1, 3),
                                  grid=BeatGrid(rng.uniform(80, 160), rng.uniform(0, 0.5)),
                                  money=money)
        _contiguous(pieces, 0.0, end)
        _no_cut_inside(pieces, [ev for ev in events if ev is not money])
        assert all(p.end - p.start > 0.05 for p in pieces)


def test_cut_in_lands_on_the_motion_low():
    activity = {9: 3.0, 10: -1.0, 11: 2.0}.get
    ev = _ev(12.0, 14.0)
    assert cut_in(ev, 0.0, lambda t: activity(int(t))) == 10.0
    # Never before the floor (the end of the previous shot).
    assert cut_in(ev, 11.5, lambda t: activity(int(t))) >= 11.5


def test_ramp_steps_into_the_impact_and_holds_the_payoff():
    ev = _ev(8.0, 9.0, peak=8.6, strength=9.0)
    pieces, _ = action_pieces(0.0, 20.0, [ev], _flat, money=ev)
    speeds = [p.speed for p in pieces]
    assert 0.7 in speeds and 0.5 in speeds
    slow = pieces[speeds.index(0.5)]
    impact = pieces[speeds.index(0.5) + 1]
    assert slow.end == pytest.approx(8.6) and impact.kind == "impact" and impact.speed == 1.0
    assert impact.end - impact.start >= PAYOFF_HOLD_SEC - 1e-6
    zooms = [p.keys[0][1] for p in pieces[speeds.index(0.7): speeds.index(0.5) + 2]]
    assert zooms == [1.15, 1.3, 1.0]


def test_beat_snap_never_lands_inside_an_event():
    grid = BeatGrid(bpm=120.0, phase_sec=0.0)
    ev = _ev(4.1, 6.0)
    pieces = [Piece(0.0, 2.1), Piece(2.1, 4.1), Piece(4.1, 6.0, kind="event")]
    snapped = snap_cuts(pieces, 0.0, grid, [ev])
    assert snapped[0].end == pytest.approx(2.0)  # filler cut snaps
    assert snapped[1].end == pytest.approx(4.0)  # onto the beat BEFORE the event: fine
    pieces = [Piece(0.0, 4.2), Piece(4.2, 6.0)]
    snapped = snap_cuts(pieces, 0.0, grid, [_ev(3.8, 4.1)])
    assert snapped[0].end == pytest.approx(4.2)  # the beat at 4.0 is inside an event


def test_short_filler_joins_the_event_shot():
    ev = _ev(0.9, 3.0)
    pieces, _ = action_pieces(0.0, 5.0, [ev], _flat)
    assert pieces[0].start == 0.0 and pieces[0].kind == "event"
    assert all(p.end - p.start >= FILL_MIN_SEC - 1e-6 or p.kind != "fill" for p in pieces)


def test_event_cold_open_and_money_event():
    events = [_ev(2.0, 3.0, 2.5, 9.0), _ev(20.0, 22.0, 21.0, 6.0), _ev(30.0, 31.0, 30.5, 4.0)]
    assert money_event(events, [(10.0, 40.0)]).peak_sec == 21.0
    # The 9.0 event sits in the first 5s — not a cold open.
    assert event_cold_open(0.0, 40.0, events) == (19.5, 22.0)
    assert event_cold_open(0.0, 4.0, events) is None


def test_hype_plan_keeps_a_cold_open_whole():
    analysis = _analysis([_scene(0, 0, 30)], None).model_copy(
        update={"energy": [EnergyPoint(time_sec=i + 0.5, motion=50.0 if i == 20 else 1.0,
                                       loudness_delta=0.0) for i in range(30)]}
    )
    cold = (19.0, 21.5)
    bounds = [(0, 19.0, 21.5), (0, 0.0, 30.0)]
    plan = plan_edit("hype", bounds, _reel([0], 0.0, 30.0), analysis, ComposeConfig(), None,
                     cold_open=cold)
    assert (plan.shots[0].in_ts, plan.shots[0].out_ts, plan.shots[0].speed) == (19.0, 21.5, 1.0)
    # The ramp still happens — in the body, not inside the cold open.
    assert any(sh.speed == 0.5 for sh in plan.shots[1:])


def test_ramp_only_where_the_impact_is():
    # The event straddles this span's end (a scene boundary): no ramp here.
    ev = _ev(74.0, 77.0, peak=76.5, strength=7.4)
    pieces, _ = action_pieces(72.07, 74.07, [ev], _flat, money=ev)
    assert all(p.speed == 1.0 for p in pieces) and pieces[-1].end == pytest.approx(74.07)


def test_director_cannot_overlap_contiguous_pieces():
    from reelforge_core.compose.director import apply_director
    from reelforge_core.compose.styles import EditPlan, PlannedShot

    analysis = _analysis([_scene(2, 60.0, 80.0)], None)
    raw = {"shots": [{"index": 1, "nudge_start_sec": -0.47, "reason": "x"}], "cuts": [],
           "hook_text": None}
    # Room to give: the nudge moves the shared boundary — nothing plays twice.
    plan = EditPlan(style="hype", shots=[PlannedShot(2, 66.0, 68.0), PlannedShot(2, 68.0, 72.0)],
                    per_cut=[("cut", 0.04)])
    out, _, _ = apply_director(plan, raw, analysis)
    assert out.shots[1].in_ts == pytest.approx(67.53) and out.shots[0].out_ts == pytest.approx(67.53)
    # No room (the first piece would drop under min_shot): it stops at the boundary.
    plan = EditPlan(style="hype", shots=[PlannedShot(2, 67.0, 68.0), PlannedShot(2, 68.0, 72.0)],
                    per_cut=[("cut", 0.04)])
    out, _, _ = apply_director(plan, raw, analysis)
    assert out.shots[1].in_ts == pytest.approx(68.0) and out.shots[0].out_ts == pytest.approx(68.0)


def test_director_keeps_the_ramp_steps():
    from reelforge_core.compose.director import apply_director
    from reelforge_core.compose.styles import EditPlan, PlannedShot

    plan = EditPlan(style="hype", shots=[PlannedShot(0, 5.0, 5.6, speed=0.7),
                                         PlannedShot(0, 5.6, 6.2, speed=0.5)],
                    per_cut=[("cut", 0.04)])
    raw = {"shots": [{"index": 0, "speed": 0.5, "reason": "x"}], "cuts": [], "hook_text": None}
    out, _, _ = apply_director(plan, raw, _analysis([_scene(0, 0.0, 20.0)], None))
    assert out.shots[0].speed == 0.7
