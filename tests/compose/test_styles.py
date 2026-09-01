"""Edit Quality CP3: style grammars + the deterministic edit planner."""

from __future__ import annotations

import pytest

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.compose.styles import (
    MUSIC_MOOD_BIAS,
    STYLE_NAMES,
    plan_edit,
    resolve_style,
)
from reelforge_core.models import (
    ComposeConfig,
    EnergyPoint,
    TransitionStyle,
)
from tests.compose.test_speech_snap import _analysis, _reel, _scene
from tests.compose.test_jumpcuts import _speech_with_gaps


def _energy(motions: list[float]):
    return [
        EnergyPoint(time_sec=i + 0.5, motion=m, loudness_delta=0.0)
        for i, m in enumerate(motions)
    ]


def _auto_config(**kw) -> ComposeConfig:
    return ComposeConfig(
        smart_mode=True, transition=TransitionStyle(kind="auto"), **kw
    )


def _manual_config(**kw) -> ComposeConfig:
    return ComposeConfig(
        smart_mode=True, transition=TransitionStyle(kind="fade"), **kw
    )


# ---- style resolution ------------------------------------------------------


def test_explicit_style_always_wins():
    analysis = _analysis([_scene(0, 0, 30)], None)
    reel = _reel([0], 0.0, 30.0)
    cfg = _manual_config(style="hype")  # manual transition, explicit style
    assert resolve_style(cfg, reel, analysis) == "hype"


def test_manual_flow_stays_classic():
    analysis = _analysis([_scene(0, 0, 30)], None)
    reel = _reel([0], 0.0, 30.0)
    assert resolve_style(_manual_config(), reel, analysis) == "classic"
    assert resolve_style(ComposeConfig(smart_mode=False), reel, analysis) == "classic"


def test_auto_classifies_talky_footage():
    analysis = _analysis([_scene(0, 0, 14)], None).model_copy(
        update={"transcript": _speech_with_gaps()}
    )
    reel = _reel([0], 0.0, 14.0)
    assert resolve_style(_auto_config(), reel, analysis) == "talking_head"


def test_auto_classifies_high_energy_as_hype():
    analysis = _analysis([_scene(0, 0, 30)], None).model_copy(
        update={"energy": _energy([1.0] * 29 + [50.0])}
    )
    reel = _reel([0], 0.0, 30.0)
    assert resolve_style(_auto_config(), reel, analysis) == "hype"


def test_auto_prefers_ranker_classification():
    analysis = _analysis([_scene(0, 0, 30)], None)
    reel = _reel([0], 0.0, 30.0).model_copy(update={"edit_style": "chill"})
    assert resolve_style(_auto_config(), reel, analysis) == "chill"
    # But an explicit config style still beats the ranker's pick.
    assert resolve_style(_manual_config(style="hype"), reel, analysis) == "hype"


def test_auto_falls_back_to_cinematic():
    analysis = _analysis([_scene(0, 0, 30)], None)
    reel = _reel([0], 0.0, 30.0)
    assert resolve_style(_auto_config(), reel, analysis) == "cinematic"


def test_music_bias_map():
    assert MUSIC_MOOD_BIAS == {"hype": "energetic", "chill": "calm"}
    assert set(MUSIC_MOOD_BIAS) < set(STYLE_NAMES)


# ---- classic is identity ---------------------------------------------------


def test_classic_plan_is_identity():
    analysis = _analysis([_scene(0, 0, 15), _scene(1, 15, 30)], None)
    reel = _reel([0, 1], 0.0, 30.0)
    bounds = [(0, 0.0, 15.0), (1, 15.0, 30.0)]
    plan = plan_edit("classic", bounds, reel, analysis, ComposeConfig(), None)
    assert [(s.scene_index, s.in_ts, s.out_ts) for s in plan.shots] == bounds
    assert all(s.speed == 1.0 and s.punch_in is None and not s.force_ken_burns for s in plan.shots)
    assert plan.per_cut == [None]
    assert plan.caption_mode is None


# ---- hype ------------------------------------------------------------------


def test_hype_places_cuts_on_beats_and_slow_mos_the_peak():
    # 30s single scene; beats every 0.5s; flat energy with one huge peak.
    analysis = _analysis([_scene(0, 0, 30)], None).model_copy(
        update={"energy": _energy([1.0] * 10 + [50.0] + [1.0] * 19)}  # peak t=10.5
    )
    reel = _reel([0], 0.0, 30.0)
    grid = BeatGrid(bpm=120.0, phase_sec=0.0)
    plan = plan_edit("hype", [(0, 0.0, 30.0)], reel, analysis, ComposeConfig(), grid)

    assert len(plan.shots) > 5  # the long take got cut up
    # Source cut points land on the 0.5s beat grid (speed shifts none here
    # before the peak piece).
    first_cuts = [s.out_ts for s in plan.shots[:3]]
    for c in first_cuts:
        assert abs(grid.snap(c) - c) < 1e-6
    # Exactly one slow-mo, on the piece containing the peak, with a drifting
    # punch-in.
    slow = [s for s in plan.shots if s.speed == 0.5]
    assert len(slow) == 1
    assert slow[0].in_ts <= 10.5 <= slow[0].out_ts
    assert slow[0].punch_in == 1.2 and slow[0].punch_in_animated
    # Intra-scene boundaries are hard cuts.
    kinds = {c[0] for c in plan.per_cut if c is not None}
    assert "cut" in kinds
    assert any("slow-mo" in n for n in plan.notes)


def test_hype_speeds_through_lulls():
    # First half energetic, second half dead: lull pieces run 1.5x.
    analysis = _analysis([_scene(0, 0, 30)], None).model_copy(
        update={"energy": _energy([10.0] * 15 + [0.0] * 15)}
    )
    reel = _reel([0], 0.0, 30.0)
    plan = plan_edit("hype", [(0, 0.0, 30.0)], reel, analysis, ComposeConfig(), None)
    lulls = [s for s in plan.shots if s.speed == 1.5]
    assert lulls and all(s.in_ts >= 14.0 for s in lulls)


def test_hype_without_grid_or_energy_still_splits():
    analysis = _analysis([_scene(0, 0, 30)], None)
    reel = _reel([0], 0.0, 30.0)
    plan = plan_edit("hype", [(0, 0.0, 30.0)], reel, analysis, ComposeConfig(), None)
    assert len(plan.shots) > 5
    assert all(s.speed == 1.0 for s in plan.shots)


def test_hype_scene_boundary_gets_slide():
    analysis = _analysis([_scene(0, 0, 3), _scene(1, 3, 6)], None)
    reel = _reel([0, 1], 0.0, 6.0)
    plan = plan_edit(
        "hype", [(0, 0.0, 3.0), (1, 3.0, 6.0)], reel, analysis, ComposeConfig(), None
    )
    assert plan.per_cut == [("slideleft", 0.25)]


# ---- talking head ----------------------------------------------------------


def test_talking_head_jump_cuts_and_alternating_punch_ins():
    analysis = _analysis([_scene(0, 0, 14)], None).model_copy(
        update={"transcript": _speech_with_gaps()}
    )
    reel = _reel([0], 0.0, 14.0)
    plan = plan_edit(
        "talking_head", [(0, 0.0, 14.0)], reel, analysis, ComposeConfig(), None
    )
    assert len(plan.shots) == 3  # the two silences got cut
    assert [s.punch_in for s in plan.shots] == [None, 1.25, None]
    assert all(c == ("cut", 0.04) for c in plan.per_cut)
    assert plan.caption_mode == "karaoke" and plan.caption_position == "centered"


# ---- cinematic + chill -----------------------------------------------------


def test_cinematic_alternates_dissolve_fadeblack_and_forces_ken_burns():
    analysis = _analysis([_scene(0, 0, 10), _scene(1, 10, 20), _scene(2, 20, 30)], None)
    reel = _reel([0, 1, 2], 0.0, 30.0)
    bounds = [(0, 0.0, 10.0), (1, 10.0, 20.0), (2, 20.0, 30.0)]
    plan = plan_edit("cinematic", bounds, reel, analysis, ComposeConfig(), None)
    assert all(s.force_ken_burns for s in plan.shots)
    assert plan.per_cut == [("dissolve", 0.8), ("fadeblack", 0.8)]
    assert plan.caption_mode == "static"


def test_chill_uses_long_fades():
    analysis = _analysis([_scene(0, 0, 10), _scene(1, 10, 20)], None)
    reel = _reel([0, 1], 0.0, 20.0)
    plan = plan_edit(
        "chill", [(0, 0.0, 10.0), (1, 10.0, 20.0)], reel, analysis, ComposeConfig(), None
    )
    assert plan.per_cut == [("fade", 0.6)]
    assert all(s.speed == 1.0 and s.punch_in is None for s in plan.shots)


# ---- forced jump cuts on non-talking-head styles ----------------------------


def test_forced_jump_cuts_apply_to_classic():
    analysis = _analysis([_scene(0, 0, 14)], None).model_copy(
        update={"transcript": _speech_with_gaps()}
    )
    reel = _reel([0], 0.0, 14.0)
    cfg = ComposeConfig(jump_cuts="on")
    plan = plan_edit("classic", [(0, 0.0, 14.0)], reel, analysis, cfg, None)
    assert len(plan.shots) == 3
    assert plan.per_cut == [("cut", 0.04), ("cut", 0.04)]
    assert plan.style == "classic"


# ---- hierarchical render chunking ------------------------------------------


def test_chunk_slices_cover_and_bound():
    from reelforge_core.compose.pipeline import CHUNK_SIZE, _chunk_slices

    for n in (1, 5, 6, 7, 11, 12, 23):
        slices = _chunk_slices(n)
        assert slices[0][0] == 0 and slices[-1][1] == n
        assert all(b - a <= CHUNK_SIZE for a, b in slices)
        # Contiguous, no overlap.
        for (a1, b1), (a2, b2) in zip(slices, slices[1:]):
            assert b1 == a2
