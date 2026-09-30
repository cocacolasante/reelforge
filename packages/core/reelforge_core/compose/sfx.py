"""Sound effects: a few, placed like an editor would, mixed under the voice.

Cues come from the edit itself — a whoosh into a B-roll cutaway or across
the one flashy transition a reel may have, a pop on an AI-picked key moment
(compose/emphasis.py) — and are thinned to at most one per SHORT_GAP_SEC
(LONG_GAP_SEC on long-form), whooshes first. They are mixed in the FINAL
render pass only, after voice + music and before the final loudnorm, so the
level target and true-peak ceiling still hold. Durations never change.

The files are synthesized at image build time (`assets/sfx/
synthesize_sfx.sh`, like the LUTs: no binary blobs, no licence to track).
Drop a real `whoosh.wav` / `pop.wav` / `hit.wav` into `/data/sfx/` to use it
instead.
"""

from __future__ import annotations

import os
from pathlib import Path

SFX_KINDS = ("pop", "whoosh", "hit")
SHORT_GAP_SEC = 6.0
LONG_GAP_SEC = 20.0
LONG_FORM_SEC = 180.0
EDGE_SEC = 0.5  # nothing in the first/last half second
# Where each file's energy peaks, so the peak — not the file start — lands
# on the event (a whoosh swells into the cut).
PEAK_SEC = {"whoosh": 0.35, "pop": 0.0, "hit": 0.0}
MAX_CUES = 40

BUNDLED_DIR = Path(os.environ.get("REELFORGE_SFX_DIR", "/app/assets/sfx"))


def sfx_path(kind: str, data_dir: Path | None = None) -> Path | None:
    """The user's file for this kind if there is one, else the bundled one."""
    candidates = []
    if data_dir is not None:
        candidates.append(Path(data_dir) / "sfx" / f"{kind}.wav")
    candidates.append(BUNDLED_DIR / f"{kind}.wav")
    for p in candidates:
        if p.is_file():
            return p
    return None


def cut_times(durations: list[float], transitions: list[tuple[str, float]]) -> list[tuple[float, str]]:
    """(mezzanine midpoint, kind) of every cut."""
    out: list[tuple[float, str]] = []
    t = 0.0
    for i, (kind, xf) in enumerate(transitions):
        if i >= len(durations) - 1:
            break
        t += durations[i] - xf  # the xfade into shot i+1 starts here
        out.append((round(t + xf / 2.0, 3), kind))
    return out


def plan_sfx(
    *,
    total: float,
    pops: list[float],
    durations: list[float],
    transitions: list[tuple[str, float]],
    layer_starts: list[float],
    whooshes: list[float] | None = None,
) -> list[tuple[float, str]]:
    """(file start in mezzanine seconds, kind), sorted — at most one per gap.

    Pure. `pops` are key-moment times; whooshes go on B-roll entries and on
    flashy transitions (a hard cut is left dry: a whoosh on every cut is the
    amateur tell the plan is avoiding)."""
    from reelforge_core.compose.director import FLASHY_KINDS

    gap = LONG_GAP_SEC if total > LONG_FORM_SEC else SHORT_GAP_SEC
    events: list[tuple[int, float, str]] = []  # (priority, event time, kind)
    for t in [*layer_starts, *(whooshes or [])]:
        events.append((0, t, "whoosh"))
    for t, kind in cut_times(durations, transitions):
        if kind in FLASHY_KINDS:
            events.append((0, t, "whoosh"))
    for t in pops:
        events.append((1, t, "pop"))
    kept: list[tuple[float, str]] = []
    for _prio, t, kind in sorted(events):
        if not EDGE_SEC <= t <= total - EDGE_SEC:
            continue
        if any(abs(t - k) < gap for k, _ in kept):
            continue
        kept.append((t, kind))
    cap = max(1, int(total // gap))
    kept = sorted(kept)[: min(cap, MAX_CUES)]
    return [(round(max(0.0, t - PEAK_SEC.get(kind, 0.0)), 3), kind) for t, kind in kept]


def sfx_enabled(setting: str, smart_mode: bool) -> bool:
    return setting == "on" or (setting == "auto" and smart_mode)


def cold_open_exit(clips: list, transitions: list[tuple[str, float]], plan) -> list[float]:
    """The mezzanine time of the cut out of a cold open (compose/styles.py
    lock_cold_open), as a one-item list for plan_sfx's `whooshes`."""
    k = getattr(plan, "cold_open_shots", 0) if plan is not None else 0
    if not k or k >= len(clips):
        return []
    cuts = cut_times([c.duration for c in clips], transitions)
    return [cuts[k - 1][0]] if k - 1 < len(cuts) else []
