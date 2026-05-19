"""Tests for compose/auto.py — smart-mode pickers and resolver."""

from __future__ import annotations

import pytest

from reelforge_core.compose.auto import (
    LUT_BY_MOOD,
    TRANSITION_BY_MOOD,
    describe_smart_picks,
    pick_lut_id,
    pick_transition_kind,
    pick_transition_kind_for_montage,
    resolve_smart_config,
)
from reelforge_core.models import (
    ComposeConfig,
    EffectsConfig,
    Mood,
    RankedReel,
    ReelScores,
    TransitionStyle,
)


def _reel(mood: Mood = "neutral") -> RankedReel:
    return RankedReel(
        candidate_id="abc",
        scene_indices=[0, 1],
        start_sec=0.0,
        end_sec=30.0,
        duration_sec=30.0,
        title="t",
        hook="h",
        justification="j",
        scores=ReelScores(
            narrative_coherence=70,
            hook_strength=80,
            emotional_payoff=60,
            standalone_clarity=70,
        ),
        overall=72.0,
        rank=1,
        suggested_mood=mood,
    )


def test_pick_transition_kind_every_mood_maps_to_known_xfade_value():
    valid = {"fade", "fadeblack", "slideleft", "wipeleft", "dissolve", "cut"}
    for mood in TRANSITION_BY_MOOD:
        kind = pick_transition_kind(_reel(mood))  # type: ignore[arg-type]
        assert kind in valid


def test_pick_lut_id_returns_known_or_none():
    valid = {None, "warm", "cool", "cinematic", "vivid"}
    for mood in LUT_BY_MOOD:
        assert pick_lut_id(_reel(mood)) in valid  # type: ignore[arg-type]


def test_pick_transition_kind_specific_moods():
    assert pick_transition_kind(_reel("energetic")) == "slideleft"
    assert pick_transition_kind(_reel("calm")) == "fade"
    assert pick_transition_kind(_reel("joyful")) == "dissolve"


def test_pick_lut_specific_moods():
    assert pick_lut_id(_reel("calm")) == "warm"
    assert pick_lut_id(_reel("joyful")) == "vivid"
    assert pick_lut_id(_reel("neutral")) is None


def test_pick_transition_kind_for_montage_majority_wins():
    # 3 energetic vs 1 calm → energetic wins → slideleft.
    assert (
        pick_transition_kind_for_montage(
            ["energetic", "energetic", "energetic", "calm"]
        )
        == "slideleft"
    )


def test_pick_transition_kind_for_montage_tie_stable():
    # Tie between calm and energetic — alphabetical breaks the tie deterministically.
    a = pick_transition_kind_for_montage(["calm", "energetic"])
    b = pick_transition_kind_for_montage(["energetic", "calm"])
    assert a == b
    # `calm` < `energetic` lexicographically, so calm wins → fade.
    assert a == "fade"


def test_pick_transition_kind_for_montage_empty_returns_fade():
    assert pick_transition_kind_for_montage([]) == "fade"


def test_resolve_smart_config_off_passthrough():
    cfg = ComposeConfig(
        smart_mode=False,
        transition=TransitionStyle(kind="auto"),
        effects=EffectsConfig(lut="auto"),
    )
    out = resolve_smart_config(cfg, _reel("energetic"))
    # smart off — auto stays literal.
    assert out.transition.kind == "auto"
    assert out.effects.lut == "auto"
    assert out is cfg  # function returns the same instance when smart is off


def test_resolve_smart_config_on_resolves_auto():
    cfg = ComposeConfig()  # smart_mode default True, transition=auto, lut=auto
    out = resolve_smart_config(cfg, _reel("energetic"))
    assert out.transition.kind == "slideleft"
    assert out.effects.lut == "vivid"
    # input must be untouched (pure resolver)
    assert cfg.transition.kind == "auto"
    assert cfg.effects.lut == "auto"


def test_resolve_smart_config_preserves_unrelated_fields():
    cfg = ComposeConfig(
        smart_mode=True,
        transition=TransitionStyle(kind="auto", duration_sec=0.75),
        video_crf=22,
        seed=42,
    )
    out = resolve_smart_config(cfg, _reel("calm"))
    assert out.transition.duration_sec == 0.75
    assert out.video_crf == 22
    assert out.seed == 42


def test_explicit_non_auto_kind_wins_over_smart():
    cfg = ComposeConfig(
        smart_mode=True,
        transition=TransitionStyle(kind="cut"),
        effects=EffectsConfig(lut="auto"),
    )
    out = resolve_smart_config(cfg, _reel("energetic"))
    assert out.transition.kind == "cut"  # user pick preserved
    assert out.effects.lut == "vivid"  # auto resolved


def test_explicit_lut_wins_over_smart():
    cfg = ComposeConfig(
        smart_mode=True,
        transition=TransitionStyle(kind="auto"),
        effects=EffectsConfig(lut="cinematic"),
    )
    out = resolve_smart_config(cfg, _reel("calm"))
    assert out.transition.kind == "fade"
    assert out.effects.lut == "cinematic"


def test_explicit_lut_none_wins_over_smart():
    cfg = ComposeConfig(
        smart_mode=True,
        effects=EffectsConfig(lut=None),
    )
    out = resolve_smart_config(cfg, _reel("energetic"))
    assert out.effects.lut is None


def test_describe_smart_picks_shape():
    cfg = ComposeConfig()
    resolved = resolve_smart_config(cfg, _reel("joyful"))
    desc = describe_smart_picks(resolved, _reel("joyful"))
    assert set(desc.keys()) == {
        "transition",
        "music",
        "lut",
        "ken_burns",
        "unsharp",
        "captions",
        "reason",
    }
    assert "joyful" in desc["reason"]
    assert "dissolve" in desc["transition"]
    assert desc["lut"] == "vivid"


def test_describe_smart_picks_no_music():
    cfg = ComposeConfig(no_music=True)
    resolved = resolve_smart_config(cfg, _reel("calm"))
    assert describe_smart_picks(resolved, _reel("calm"))["music"] == "off"
