"""Smart auto-pickers for transitions, LUTs, and effects.

Resolves the "auto" sentinel values on `ComposeConfig.transition.kind` and
`EffectsConfig.lut` based on the reel's content (mood, visual energy,
speech-heavy vs. silent). The resolver is pure — it returns a *new*
`ComposeConfig` rather than mutating the input — so the manifest persisted
in `compose.json` reflects exactly what was rendered.
"""

from __future__ import annotations

from typing import Iterable

from reelforge_core.models import (
    AnalysisReport,
    ComposeConfig,
    Mood,
    RankedReel,
    SceneSemantics,
    TransitionStyle,
)

# Transition picks are intentionally conservative — fade and dissolve work for
# almost everything; the punchier kinds (slideleft, wipeleft) only fire for
# energetic / triumphant content where snappier cuts feel right.
TRANSITION_BY_MOOD: dict[str, str] = {
    "calm": "fade",
    "tense": "fade",
    "joyful": "dissolve",
    "somber": "fadeblack",
    "energetic": "slideleft",
    "mysterious": "fadeblack",
    "romantic": "dissolve",
    "triumphant": "slideleft",
    "melancholic": "fadeblack",
    "neutral": "fade",
}

# LUTs are subtle — applied last, after captions are burned. Names match the
# files bundled under /app/assets/luts/.
LUT_BY_MOOD: dict[str, str | None] = {
    "calm": "warm",
    "tense": "cinematic",
    "joyful": "vivid",
    "somber": "cool",
    "energetic": "vivid",
    "mysterious": "cool",
    "romantic": "warm",
    "triumphant": "cinematic",
    "melancholic": "cool",
    "neutral": None,
}


def pick_transition_kind(reel: RankedReel) -> str:
    return TRANSITION_BY_MOOD.get(reel.suggested_mood, "fade")


def pick_lut_id(reel: RankedReel) -> str | None:
    return LUT_BY_MOOD.get(reel.suggested_mood, None)


def pick_transition_kind_for_montage(child_moods: Iterable[str]) -> str:
    """Pick one transition for a montage based on the dominant mood across
    its chapters. Uses a stable tie-breaker so the same montage always picks
    the same kind."""
    counts: dict[str, int] = {}
    for m in child_moods:
        counts[m] = counts.get(m, 0) + 1
    if not counts:
        return "fade"
    dominant = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return TRANSITION_BY_MOOD.get(dominant, "fade")


def resolve_smart_config(
    config: ComposeConfig,
    reel: RankedReel,
    analysis: AnalysisReport | None = None,
) -> ComposeConfig:
    """Return a `ComposeConfig` with `auto` sentinels resolved to concrete
    picks. If `smart_mode` is False, the input is returned unchanged.

    Sentinel rules:
      - `transition.kind == "auto"` → pick_transition_kind(reel)
      - `effects.lut == "auto"`     → pick_lut_id(reel)

    Even when `smart_mode` is True but the user explicitly set a non-auto
    value, the explicit choice wins. This is what makes the auto toggle a
    *default* and not a override.
    """
    if not config.smart_mode:
        return config

    new_transition = config.transition
    if config.transition.kind == "auto":  # type: ignore[comparison-overlap]
        new_transition = TransitionStyle(
            kind=pick_transition_kind(reel),  # type: ignore[arg-type]
            duration_sec=config.transition.duration_sec,
        )

    new_effects = config.effects
    if config.effects.lut == "auto":
        # model_copy keeps every other effects field (a field-by-field rebuild
        # here once silently dropped `reframe`, reverting user crop choices).
        new_effects = config.effects.model_copy(update={"lut": pick_lut_id(reel)})

    return config.model_copy(
        update={"transition": new_transition, "effects": new_effects}
    )


def smart_picks_for_mood(mood: str) -> dict[str, str | None]:
    """The mood→style picks as data — the single source of truth the API
    serves to the web UI (which previously duplicated these tables in TS)."""
    return {
        "transition": TRANSITION_BY_MOOD.get(mood, "fade"),
        "lut": LUT_BY_MOOD.get(mood),
    }


def describe_smart_picks(resolved: ComposeConfig, reel: RankedReel) -> dict[str, str]:
    """Return a human-readable description of what the smart picker chose.

    Surfaced to the UI so the user sees what the AI did. The same info is
    persisted on `ComposeManifest.config` since it's just the resolved config.
    """
    music = "auto-matched" if not resolved.music_track_id else resolved.music_track_id
    if resolved.no_music:
        music = "off"
    return {
        "transition": f"{resolved.transition.kind} ({resolved.transition.duration_sec:.2f}s)",
        "music": music,
        "lut": resolved.effects.lut or "none",
        "ken_burns": "on (low-energy scenes)" if resolved.effects.ken_burns_on_low_energy else "off",
        "unsharp": "on" if resolved.effects.unsharp else "off",
        "captions": resolved.captions.mode,
        "reason": f"mood={reel.suggested_mood}",
    }
