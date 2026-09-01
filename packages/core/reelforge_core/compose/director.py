"""AI edit-director: one small call refining the deterministic edit plan.

The style grammar (compose/styles.py) produces a complete, correct plan; the
director looks at the actual content and proposes adjustments WITHIN the
grammar's bounds — a better transition for a specific cut, a nudged cut
point, a ramp or punch-in on a moment the heuristics missed, and an optional
hook text overlay for the opening two seconds.

Every proposal is validated locally by pure `apply_director` (the live rule
from CP7-of-Selection-v2: models DO violate stated constraints):
  - shot nudges clamp to ±NUDGE_MAX_SEC, stay inside the asset, keep the
    style's minimum shot length, and snap speech-safe;
  - speeds must come from the style's allowed set; punch-ins cap at 1.5;
  - cut kinds must come from the style's palette; durations cap per style;
  - anything invalid reverts to the deterministic plan, entry by entry.

The call is stamped (plan hash + style + model + prompt version) into the
reel dir, so re-composes with unchanged inputs cost zero tokens. Failure of
any kind keeps the deterministic plan — compose never blocks on the director.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.compose.styles import EditPlan, PlannedShot
from reelforge_core.models import (
    AnalysisReport,
    ComposeConfig,
    RankedReel,
    TextOverlay,
    UsageTotals,
)

log = logging.getLogger(__name__)

DIRECTOR_PROMPT_VERSION = "d1"
NUDGE_MAX_SEC = 1.5
PUNCH_IN_MAX = 1.5
HOOK_TEXT_MAX = 40

# Per-style validation bounds. "cut" is always an allowed kind.
STYLE_BOUNDS: dict[str, dict[str, Any]] = {
    "hype": {
        "palette": {"cut", "slideleft", "slideright", "fadewhite", "smoothleft", "smoothright"},
        "max_cut_dur": 0.35,
        "speeds": {0.5, 1.0, 1.5, 2.0},
        "min_shot": 0.6,
    },
    "talking_head": {
        "palette": {"cut"},
        "max_cut_dur": 0.05,
        "speeds": {1.0},
        "min_shot": 0.4,
    },
    "cinematic": {
        "palette": {"fade", "dissolve", "fadeblack", "circleopen", "circleclose"},
        "max_cut_dur": 1.2,
        "speeds": {0.5, 1.0},
        "min_shot": 2.0,
    },
    "chill": {
        "palette": {"fade", "dissolve"},
        "max_cut_dur": 1.0,
        "speeds": {1.0},
        "min_shot": 2.0,
    },
    "classic": {
        "palette": {"cut", "fade", "fadeblack", "dissolve", "slideleft", "wipeleft"},
        "max_cut_dur": 1.2,
        "speeds": {1.0},
        "min_shot": 1.0,
    },
}

DIRECTOR_SYSTEM_PROMPT = (
    "You are the edit director for a short-form reel. A deterministic style "
    "grammar has already produced a complete edit plan; you refine it based "
    "on the actual content. Propose ONLY changes that clearly improve the "
    "edit — empty lists are a perfectly good answer.\n\n"
    "You may:\n"
    "- nudge a shot's start/end by up to 1.5s (e.g. open a beat earlier on "
    "the action, end before it fizzles);\n"
    "- change a cut's transition WITHIN the style palette given to you;\n"
    "- set a shot's speed (only values from the allowed set) or a punch-in "
    "(1.0-1.5) where the content earns it;\n"
    "- write hook_text: at most 40 characters burned over the first two "
    "seconds — concrete and curiosity-driving, or null if the opening "
    "speaks for itself.\n\n"
    "Respect the constraints block verbatim — out-of-bounds proposals are "
    "discarded. Call record_edit_plan exactly once."
)

RECORD_EDIT_PLAN: dict[str, Any] = {
    "name": "record_edit_plan",
    "description": "Record refinements to the deterministic edit plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "shots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "nudge_start_sec": {"type": "number", "minimum": -1.5, "maximum": 1.5},
                        "nudge_end_sec": {"type": "number", "minimum": -1.5, "maximum": 1.5},
                        "speed": {"type": ["number", "null"]},
                        "punch_in": {"type": ["number", "null"]},
                        "punch_in_animated": {"type": "boolean"},
                        "reason": {"type": "string", "maxLength": 120},
                    },
                    "required": ["index", "reason"],
                },
            },
            "cuts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "kind": {"type": "string"},
                        "duration_sec": {"type": "number", "minimum": 0.04, "maximum": 1.5},
                        "reason": {"type": "string", "maxLength": 120},
                    },
                    "required": ["index", "kind", "reason"],
                },
            },
            "hook_text": {"type": ["string", "null"], "maxLength": HOOK_TEXT_MAX},
        },
        "required": ["shots", "cuts", "hook_text"],
    },
}


def plan_fingerprint(plan: EditPlan, style: str, model: str) -> str:
    """Stable hash of everything the director's answer depends on."""
    payload = {
        "v": DIRECTOR_PROMPT_VERSION,
        "style": style,
        "model": model,
        "shots": [
            [s.scene_index, round(s.in_ts, 3), round(s.out_ts, 3), s.speed, s.punch_in]
            for s in plan.shots
        ],
        "cuts": plan.per_cut,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def build_director_context(
    plan: EditPlan,
    reel: RankedReel,
    analysis: AnalysisReport,
    beat_grid: BeatGrid | None,
) -> dict:
    """The JSON the director sees. Pure."""
    from reelforge_core.reels.generators.moment import combined_scores
    from reelforge_core.reels.rank import _span_words

    energy = combined_scores(analysis)
    sem_by_idx = {s.scene_index: s for s in analysis.semantics}
    bounds = STYLE_BOUNDS.get(plan.style, STYLE_BOUNDS["classic"])
    shots = []
    for i, s in enumerate(plan.shots):
        zs = [z for t, z in energy if s.in_ts <= t <= s.out_ts]
        sem = sem_by_idx.get(s.scene_index)
        words = _span_words(analysis.transcript, s.in_ts, s.out_ts)
        shots.append(
            {
                "index": i,
                "in_ts": round(s.in_ts, 2),
                "out_ts": round(s.out_ts, 2),
                "speed": s.speed,
                "punch_in": s.punch_in,
                "mean_energy_z": round(sum(zs) / len(zs), 2) if zs else None,
                "scene_summary": sem.summary if sem else "",
                "first_words": " ".join(w for _, w in words[:8]),
                "last_words": " ".join(w for _, w in words[-8:]) if len(words) > 8 else "",
            }
        )
    return {
        "style": plan.style,
        "constraints": {
            "transition_palette": sorted(bounds["palette"]),
            "max_transition_sec": bounds["max_cut_dur"],
            "allowed_speeds": sorted(bounds["speeds"]),
            "min_shot_sec": bounds["min_shot"],
            "max_nudge_sec": NUDGE_MAX_SEC,
        },
        "reel": {
            "title": reel.title,
            "hook": reel.hook,
            "opening_description": reel.opening_description,
        },
        "beat_bpm": round(beat_grid.bpm, 1) if beat_grid else None,
        "shots": shots,
        "cuts": [
            {"index": i, "kind": c[0] if c else "reel-default", "duration_sec": c[1] if c else None}
            for i, c in enumerate(plan.per_cut)
        ],
    }


def apply_director(
    plan: EditPlan,
    raw: dict,
    analysis: AnalysisReport,
) -> tuple[EditPlan, TextOverlay | None, list[str]]:
    """Validate + apply the director's proposals entry by entry. Pure."""
    from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start

    bounds = STYLE_BOUNDS.get(plan.style, STYLE_BOUNDS["classic"])
    shots = list(plan.shots)
    per_cut = list(plan.per_cut)
    applied: list[str] = []
    words = flatten_words(analysis.transcript) if analysis.transcript else []

    def _mid_word(t: float) -> bool:
        return any(ws < t < we for ws, we in words)

    for entry in raw.get("shots", []) or []:
        try:
            i = int(entry["index"])
            if not 0 <= i < len(shots):
                continue
            s = shots[i]
            new_in = s.in_ts + float(entry.get("nudge_start_sec") or 0.0)
            new_out = s.out_ts + float(entry.get("nudge_end_sec") or 0.0)
            new_in = min(max(new_in, s.in_ts - NUDGE_MAX_SEC), s.in_ts + NUDGE_MAX_SEC)
            new_out = min(max(new_out, s.out_ts - NUDGE_MAX_SEC), s.out_ts + NUDGE_MAX_SEC)
            new_in = max(0.0, new_in)
            new_out = min(analysis.duration, new_out)
            if words:
                if _mid_word(new_in):
                    new_in = max(0.0, snap_start(new_in, words, 0.6))
                if _mid_word(new_out):
                    new_out = min(analysis.duration, snap_end(new_out, words, 0.6))
            speed = entry.get("speed")
            speed = float(speed) if speed is not None else s.speed
            if speed not in bounds["speeds"]:
                speed = s.speed
            if new_out - new_in < bounds["min_shot"] * speed:
                new_in, new_out = s.in_ts, s.out_ts  # revert geometry, keep rest
            punch = entry.get("punch_in", s.punch_in)
            if punch is not None:
                punch = float(punch)
                if not 1.0 <= punch <= PUNCH_IN_MAX:
                    punch = s.punch_in
            changed = (
                (new_in, new_out, speed, punch)
                != (s.in_ts, s.out_ts, s.speed, s.punch_in)
            )
            if changed:
                shots[i] = replace(
                    s,
                    in_ts=round(new_in, 3),
                    out_ts=round(new_out, 3),
                    speed=speed,
                    punch_in=punch,
                    punch_in_animated=bool(entry.get("punch_in_animated", s.punch_in_animated)),
                )
                applied.append(f"shot {i}: {entry.get('reason', '')[:80]}")
        except (KeyError, TypeError, ValueError):
            continue

    palette = set(bounds["palette"]) | {"cut"}
    for entry in raw.get("cuts", []) or []:
        try:
            i = int(entry["index"])
            kind = str(entry["kind"])
            if not 0 <= i < len(per_cut) or kind not in palette:
                continue
            dur = min(float(entry.get("duration_sec") or 0.2), bounds["max_cut_dur"])
            dur = 0.04 if kind == "cut" else max(0.04, dur)
            per_cut[i] = (kind, round(dur, 3))
            applied.append(f"cut {i} -> {kind}: {entry.get('reason', '')[:80]}")
        except (KeyError, TypeError, ValueError):
            continue

    overlay: TextOverlay | None = None
    hook = raw.get("hook_text")
    if isinstance(hook, str) and hook.strip():
        overlay = TextOverlay(
            id="director-hook",
            text=hook.strip()[:HOOK_TEXT_MAX],
            start_sec=0.4,
            end_sec=2.8,
            position="top",
        )
        applied.append(f"hook overlay: {overlay.text!r}")

    new_plan = EditPlan(
        style=plan.style,
        shots=shots,
        per_cut=per_cut,
        caption_mode=plan.caption_mode,
        caption_position=plan.caption_position,
        notes=plan.notes + [f"director: {a}" for a in applied],
    )
    return new_plan, overlay, applied


def _extract_plan(resp: Any) -> dict:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            inp = getattr(block, "input", None)
            if isinstance(inp, str):
                inp = json.loads(inp)
            if isinstance(inp, dict) and "shots" in inp:
                return inp
    raise ValueError("no record_edit_plan tool_use block in response")


async def run_director(
    plan: EditPlan,
    reel: RankedReel,
    analysis: AnalysisReport,
    config: ComposeConfig,
    beat_grid: BeatGrid | None,
    reel_dir: Path,
    *,
    client: Any | None = None,
) -> tuple[EditPlan, TextOverlay | None, UsageTotals]:
    """Stamped, best-effort director pass. Returns (plan, hook overlay, usage);
    on any failure the incoming plan comes back untouched."""
    from reelforge_core.io_utils import write_json_atomic
    from reelforge_core.reels.rank import _accumulate_usage, _call_model

    raw_path = reel_dir / "director_raw.json"
    stamp_path = reel_dir / "director_raw.json.stamp"
    fingerprint = plan_fingerprint(plan, plan.style, config.director_model)

    if raw_path.exists() and stamp_path.exists():
        try:
            if stamp_path.read_text().strip() == fingerprint:
                raw = json.loads(raw_path.read_text()).get("plan", {})
                new_plan, overlay, applied = apply_director(plan, raw, analysis)
                log.info("director: stamp hit (%d adjustment(s) replayed)", len(applied))
                return new_plan, overlay, UsageTotals()
        except Exception:  # pragma: no cover — a corrupt cache just re-runs
            pass

    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
    context = build_director_context(plan, reel, analysis, beat_grid)
    try:
        resp = await _call_model(
            client,
            model=config.director_model,
            temperature=0.0,
            system_prompt=DIRECTOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(context, indent=2)}],
            tools=[RECORD_EDIT_PLAN],
            tool_name="record_edit_plan",
            max_tokens=4000,
        )
        raw = _extract_plan(resp)
        usage = _accumulate_usage(resp)
    except Exception as exc:
        log.warning("edit director failed; keeping deterministic plan: %s", exc)
        return plan, None, UsageTotals()

    new_plan, overlay, applied = apply_director(plan, raw, analysis)
    write_json_atomic(raw_path, {"plan": raw, "usage": usage.model_dump()})
    stamp_path.write_text(fingerprint, encoding="utf-8")
    log.info(
        "director: %d proposal(s) applied (in %d / out %d tokens)",
        len(applied),
        usage.input_tokens,
        usage.output_tokens,
    )
    return new_plan, overlay, usage
