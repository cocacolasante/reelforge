"""Edit Quality CP0: smart-config reframe fix, timeline Ken Burns, xfade clamp."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.auto import resolve_smart_config, smart_picks_for_mood
from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph_builder import build_final_command, clamp_transitions
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    TransitionStyle,
)
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _vclip(idx: int, in_ts: float, out_ts: float, force_kb: bool = False) -> ClipInfo:
    return ClipInfo(
        path=Path(f"/tmp/clip_{idx:04d}.mp4"),
        scene_index=-1,
        in_ts=in_ts,
        out_ts=out_ts,
        duration=out_ts - in_ts,
        has_audio=True,
        effects_applied=[],
        asset_id="a",
        force_ken_burns=force_kb,
    )


# ---- bug 1: reframe must survive auto-lut resolution ------------------------


def test_resolve_smart_config_preserves_reframe_on_auto_lut():
    reel = _reel([0], 0.0, 30.0).model_copy(update={"suggested_mood": "calm"})
    config = ComposeConfig(
        smart_mode=True,
        effects=EffectsConfig(lut="auto", reframe="letterbox", unsharp=False),
    )
    resolved = resolve_smart_config(config, reel)
    assert resolved.effects.lut == "warm"  # calm -> warm
    assert resolved.effects.reframe == "letterbox"  # previously reverted to "auto"
    assert resolved.effects.unsharp is False


def test_smart_picks_for_mood_matches_tables():
    assert smart_picks_for_mood("energetic") == {"transition": "slideleft", "lut": "vivid"}
    assert smart_picks_for_mood("neutral") == {"transition": "fade", "lut": None}
    assert smart_picks_for_mood("not-a-mood") == {"transition": "fade", "lut": None}


# ---- bug 2: TimelineShot.ken_burns must render for video shots --------------


def test_force_ken_burns_renders_for_timeline_video_shot():
    clips = [_vclip(0, 0, 10, force_kb=True), _vclip(1, 10, 20)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis([_scene(0, 0, 30)], None),
        music_path=None,
        captions_path=None,
        # Low-energy auto-trigger OFF: only the per-shot flag can fire it.
        config=ComposeConfig(
            captions=CaptionStyle(mode="off"),
            effects=EffectsConfig(ken_burns_on_low_energy=False, unsharp=False, lut=None),
        ),
        output_path=Path("/tmp/out.mp4"),
        transitions=[("fade", 0.4)],
    )
    fc = plan.filter_complex
    # Clip 0 gets the scale+crop drift (the expression appears once for x and
    # once for y); clip 1 stays a null rename.
    assert "crop=" in fc and "min(t/" in fc
    assert fc.count("(iw-ow)*") == 1  # exactly one Ken Burns clip


# ---- bug 3: xfade never outlasts its shorter neighbour ----------------------


def test_clamp_transitions_clamps_long_xfade_on_short_shot():
    # 0.3s middle shot with 0.4s fades on both sides -> each clamped to 0.15.
    out = clamp_transitions([10.0, 0.3, 10.0], [("fade", 0.4), ("fade", 0.4)])
    assert out == [("fade", 0.15), ("fade", 0.15)]


def test_clamp_transitions_leaves_safe_durations_alone():
    trans = [("slideleft", 0.4), ("dissolve", 1.0)]
    assert clamp_transitions([10.0, 8.0, 10.0], trans) == trans


def test_clamp_transitions_floors_at_cut_duration():
    out = clamp_transitions([0.05, 0.05], [("fade", 0.4)])
    assert out == [("fade", 0.04)]


# ---- dead config removed ----------------------------------------------------


def test_burn_title_card_is_gone():
    assert not hasattr(ComposeConfig(), "burn_title_card")
    # seed stays — music selection is keyed on it.
    assert ComposeConfig().seed == 1


def test_transition_style_unchanged_defaults():
    t = TransitionStyle()
    assert t.kind == "auto" and t.duration_sec == 0.4
