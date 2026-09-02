"""AI Mix CP1: multi-source style planning into a ReelTimeline."""

from __future__ import annotations

import pytest

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.mixes.planner import MAX_MIX_SHOTS, plan_mix
from reelforge_core.models import ComposeConfig, EnergyPoint, ReelTimeline

from tests.compose.test_jumpcuts import _speech_with_gaps
from tests.reels._fixtures import make_analysis


A1, A2 = "a" * 64, "b" * 64


def _analyses(energy_for: str | None = None):
    out = {A1: make_analysis(A1, [60.0]), A2: make_analysis(A2, [60.0])}
    if energy_for:
        pts = [EnergyPoint(time_sec=i + 0.5, motion=1.0, loudness_delta=0.0) for i in range(60)]
        pts[20] = EnergyPoint(time_sec=20.5, motion=90.0, loudness_delta=0.0)
        out[energy_for] = out[energy_for].model_copy(update={"energy": pts})
    return out


def _conforms(tl: ReelTimeline) -> None:
    """The generated timeline must satisfy every editor PUT rule."""
    assert 1 <= len(tl.shots) <= MAX_MIX_SHOTS
    for s in tl.shots:
        assert s.kind == "video"
        assert s.out_ts - s.in_ts >= 0.15
        assert s.in_ts >= 0.0
        if s.transition_after is not None:
            assert s.transition_after.kind != "auto"
    # Round-trips through ComposeConfig like the compose endpoint does.
    cfg = ComposeConfig(timeline=tl)
    assert ComposeConfig(**cfg.model_dump()).timeline is not None


def test_classic_plain_cuts():
    tl = plan_mix([(A1, 0.0, 5.0), (A2, 10.0, 15.0)], _analyses(), "classic", None)
    _conforms(tl)
    assert tl.shots[0].transition_after.kind == "cut"
    assert tl.shots[-1].transition_after is None
    assert all(s.speed == 1.0 and not s.ken_burns for s in tl.shots)


def test_cinematic_alternates_and_kens():
    shots = [(A1, 0.0, 5.0), (A2, 0.0, 5.0), (A1, 10.0, 15.0)]
    tl = plan_mix(shots, _analyses(), "cinematic", None)
    _conforms(tl)
    assert [s.transition_after.kind for s in tl.shots[:-1]] == ["dissolve", "fadeblack"]
    assert all(s.ken_burns for s in tl.shots)


def test_chill_long_fades():
    tl = plan_mix([(A1, 0.0, 5.0), (A2, 0.0, 5.0)], _analyses(), "chill", None)
    _conforms(tl)
    assert tl.shots[0].transition_after.kind == "fade"
    assert tl.shots[0].transition_after.duration_sec == 0.6


def test_hype_source_change_slides_same_source_cuts():
    shots = [(A1, 0.0, 3.0), (A1, 10.0, 13.0), (A2, 0.0, 3.0), (A1, 20.0, 23.0)]
    tl = plan_mix(shots, _analyses(), "hype", None)
    _conforms(tl)
    kinds = [s.transition_after.kind for s in tl.shots[:-1]]
    # a->a cut, a->b slide, b->a slide (alternating direction).
    assert kinds == ["cut", "slideleft", "slideright"]


def test_hype_beat_splits_long_moments():
    grid = BeatGrid(bpm=120.0, phase_sec=0.0)
    tl = plan_mix([(A1, 0.0, 12.0)], _analyses(), "hype", grid)
    _conforms(tl)
    assert len(tl.shots) > 1
    # Intra-moment boundaries are hard cuts on the beat grid.
    for s in tl.shots[:-1]:
        assert s.transition_after.kind == "cut"
        assert abs(grid.snap(s.out_ts) - s.out_ts) < 1e-6


def test_hype_slow_mo_on_global_peak():
    analyses = _analyses(energy_for=A2)
    shots = [(A1, 0.0, 3.0), (A2, 19.0, 22.0), (A1, 10.0, 13.0)]
    tl = plan_mix(shots, analyses, "hype", None)
    _conforms(tl)
    slow = [s for s in tl.shots if s.speed == 0.5]
    assert len(slow) == 1
    assert slow[0].asset_id == A2 and slow[0].in_ts <= 20.5 <= slow[0].out_ts
    assert slow[0].punch_in == 1.2 and slow[0].punch_in_animated
    # Sped shots mute their own audio (model rule).
    assert slow[0].effective_gain == 0.0


def test_hype_respects_shot_cap():
    # 30 x 12s moments would beat-split into ~150 pieces; the planner stops
    # splitting instead of exceeding the editor cap.
    shots = [(A1, float(i), float(i) + 12.0) for i in range(0, 30)]
    tl = plan_mix(shots, _analyses(), "hype", BeatGrid(bpm=120.0, phase_sec=0.0))
    _conforms(tl)
    assert len(tl.shots) <= MAX_MIX_SHOTS


def test_talking_head_jump_cuts_across_sources():
    analyses = _analyses()
    analyses[A1] = analyses[A1].model_copy(update={"transcript": _speech_with_gaps()})
    shots = [(A1, 0.0, 14.0), (A2, 0.0, 5.0)]
    tl = plan_mix(shots, analyses, "talking_head", None)
    _conforms(tl)
    # A1's moment split around its two silences -> 3 pieces + A2's whole.
    assert len(tl.shots) == 4
    assert [s.punch_in for s in tl.shots] == [None, 1.25, None, 1.25]
    assert all(
        s.transition_after.kind == "cut" for s in tl.shots[:-1]
    )
