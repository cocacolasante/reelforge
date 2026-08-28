"""Boundary refinement for the top-K reels (one small API call, best-effort).

The ranker picks WHICH span wins; this pass nudges the edges of the winners —
open on the strongest line, don't clip a word, land on a natural seam. All
returned bounds are validated locally by the pure `apply_refinement`:

  - each edge clamps to ±REFINE_WINDOW_SEC of its original,
  - the refined duration must stay within the config's effective window
    (violations revert both edges),
  - with speech present, a mid-word edge is snapped (max nudge 0.6s) and
    reverted if still mid-word,
  - `scene_indices` are recomputed; `candidate_id` NEVER changes (it is the
    reel's identity downstream) — the original bounds are kept in
    `pre_refine_start_sec` / `pre_refine_end_sec`.

Refinement failing (API error, malformed response) keeps the unrefined bounds:
selection always completes.
"""

from __future__ import annotations

import logging
from typing import Any

from reelforge_core.models import (
    AnalysisReport,
    RankedReel,
    SelectionConfig,
    UsageTotals,
)

log = logging.getLogger(__name__)

REFINE_PROMPT_VERSION = "r1"
REFINE_WINDOW_SEC = 6.0
SNAP_MAX_NUDGE_SEC = 0.6

REFINE_SYSTEM_PROMPT = (
    "You are a senior short-form video editor fine-tuning the cut points of "
    "already-selected reels. For each reel you get its current bounds, the "
    "word-timestamped transcript around each edge, utterance-boundary times, "
    "per-second energy, and the reel's hook and opening description.\n\n"
    "Propose new_start_sec / new_end_sec for each reel:\n"
    "- Move each edge at most 6 seconds from its current position.\n"
    "- Keep the duration within the allowed range given per reel.\n"
    "- When speech is present, land edges on utterance boundaries — never "
    "mid-word. Prefer OPENING on the strongest line: if a better hook line "
    "starts just before or after the current start, move to it.\n"
    "- Without speech, prefer edges where energy is low (a lull) and keep the "
    "peak inside the reel, early rather than late.\n"
    "- If the current bounds are already right, return them unchanged.\n\n"
    "Call the record_refinements tool exactly once with an entry for EVERY "
    "reel. reason: one short sentence."
)

RECORD_REFINEMENTS: dict[str, Any] = {
    "name": "record_refinements",
    "description": "Record refined cut points for every reel.",
    "input_schema": {
        "type": "object",
        "properties": {
            "refinements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "new_start_sec": {"type": "number", "minimum": 0},
                        "new_end_sec": {"type": "number", "minimum": 0},
                        "reason": {"type": "string", "maxLength": 200},
                    },
                    "required": [
                        "candidate_id",
                        "new_start_sec",
                        "new_end_sec",
                        "reason",
                    ],
                },
            }
        },
        "required": ["refinements"],
    },
}


def build_refinement_context(
    reel: RankedReel, analysis: AnalysisReport, units: list
) -> dict:
    """Everything the model needs to nudge one reel's edges. Pure."""
    from reelforge_core.reels.generators.moment import combined_scores
    from reelforge_core.reels.rank import _span_words

    lo = max(0.0, reel.start_sec - REFINE_WINDOW_SEC)
    hi = min(analysis.duration, reel.end_sec + REFINE_WINDOW_SEC)
    start_words = _span_words(
        analysis.transcript, lo, min(hi, reel.start_sec + REFINE_WINDOW_SEC)
    )
    end_words = _span_words(
        analysis.transcript, max(lo, reel.end_sec - REFINE_WINDOW_SEC), hi
    )
    unit_starts = [round(u.start, 2) for u in units if lo <= u.start <= hi]
    unit_ends = [round(u.end, 2) for u in units if lo <= u.end <= hi]
    energy = [
        [round(t, 1), round(z, 1)]
        for t, z in combined_scores(analysis)
        if lo <= t <= hi
    ]
    return {
        "candidate_id": reel.candidate_id,
        "current_start_sec": round(reel.start_sec, 2),
        "current_end_sec": round(reel.end_sec, 2),
        "start_edge_window": [round(lo, 2), round(reel.start_sec + REFINE_WINDOW_SEC, 2)],
        "end_edge_window": [round(max(0.0, reel.end_sec - REFINE_WINDOW_SEC), 2), round(hi, 2)],
        "hook": reel.hook,
        "opening_description": reel.opening_description,
        "words_near_start": [[t, w] for t, w in start_words],
        "words_near_end": [[t, w] for t, w in end_words],
        "unit_boundary_starts": unit_starts,
        "unit_boundary_ends": unit_ends,
        "energy_series": energy,
    }


def _mid_word(t: float, words: list[tuple[float, float]]) -> bool:
    return any(s < t < e for s, e in words)


def apply_refinement(
    reel: RankedReel,
    new_start: float,
    new_end: float,
    analysis: AnalysisReport,
    config: SelectionConfig,
) -> RankedReel:
    """Validate + apply one refinement. Pure; returns the (possibly
    unchanged) reel. See module docstring for the rules."""
    from reelforge_core.compose.speech_snap import snap_end, snap_start
    from reelforge_core.reels.candidates import covering_scenes
    from reelforge_core.reels.features import flatten_words

    orig_start, orig_end = reel.start_sec, reel.end_sec
    start = min(max(new_start, orig_start - REFINE_WINDOW_SEC), orig_start + REFINE_WINDOW_SEC)
    start = max(0.0, start)
    end = min(max(new_end, orig_end - REFINE_WINDOW_SEC), orig_end + REFINE_WINDOW_SEC)
    end = min(analysis.duration, end)

    if not (config.effective_min_sec <= end - start <= config.effective_max_sec):
        log.info(
            "refinement for %s rejected: duration %.1fs outside [%.0f, %.0f]",
            reel.candidate_id,
            end - start,
            config.effective_min_sec,
            config.effective_max_sec,
        )
        return reel

    if analysis.transcript is not None:
        words = flatten_words(analysis.transcript)
        if _mid_word(start, words):
            snapped = max(0.0, snap_start(start, words, SNAP_MAX_NUDGE_SEC))
            start = snapped if not _mid_word(snapped, words) else orig_start
        if _mid_word(end, words):
            snapped = min(analysis.duration, snap_end(end, words, SNAP_MAX_NUDGE_SEC))
            end = snapped if not _mid_word(snapped, words) else orig_end

    if abs(start - orig_start) < 1e-6 and abs(end - orig_end) < 1e-6:
        return reel
    if end - start < 0.5:  # degenerate after reverts
        return reel

    covered = covering_scenes(analysis.scenes, start, end)
    return reel.model_copy(
        update={
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "duration_sec": round(end - start, 6),
            "scene_indices": covered,
            "pre_refine_start_sec": orig_start,
            "pre_refine_end_sec": orig_end,
        }
    )


def _extract_refinements(resp: Any) -> list[dict]:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            inp = getattr(block, "input", None)
            if isinstance(inp, str):
                import json as _json

                inp = _json.loads(inp)
            if isinstance(inp, dict) and isinstance(inp.get("refinements"), list):
                return inp["refinements"]
    raise ValueError("no record_refinements tool_use block in response")


async def refine_reels(
    reels: list[RankedReel],
    analysis: AnalysisReport,
    config: SelectionConfig,
    *,
    client: Any | None = None,
) -> tuple[list[RankedReel], UsageTotals, list[dict]]:
    """One API call refining every reel's bounds. Returns (reels, usage,
    raw_refinements); on any failure returns the originals untouched."""
    import json

    from reelforge_core.reels.generators.sentence import build_units
    from reelforge_core.reels.rank import _accumulate_usage, _call_model

    if not reels:
        return reels, UsageTotals(), []
    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()

    units = build_units(analysis.transcript)
    contexts = [build_refinement_context(r, analysis, units) for r in reels]
    payload = {
        "duration_limits_sec": [config.effective_min_sec, config.effective_max_sec],
        "reels": contexts,
    }
    messages = [{"role": "user", "content": json.dumps(payload, indent=2)}]
    try:
        resp = await _call_model(
            client,
            model=config.ranking_model,
            temperature=config.temperature,
            system_prompt=REFINE_SYSTEM_PROMPT,
            messages=messages,
            tools=[RECORD_REFINEMENTS],
            tool_name="record_refinements",
            max_tokens=4000,
        )
        raw = _extract_refinements(resp)
        usage = _accumulate_usage(resp)
    except Exception as exc:
        log.warning("boundary refinement failed; keeping unrefined bounds: %s", exc)
        return reels, UsageTotals(), []

    return apply_refinements_raw(reels, raw, analysis, config), usage, raw


def apply_refinements_raw(
    reels: list[RankedReel],
    raw: list[dict],
    analysis: AnalysisReport,
    config: SelectionConfig,
) -> list[RankedReel]:
    """Apply a raw refinements list (fresh from the API or replayed from
    refine_raw.json). Pure; unknown/malformed entries are skipped."""
    by_id: dict[str, dict] = {}
    for entry in raw:
        cid = entry.get("candidate_id")
        if cid is not None:
            by_id[cid] = entry
    out: list[RankedReel] = []
    for reel in reels:
        entry = by_id.get(reel.candidate_id)
        if entry is None:
            out.append(reel)
            continue
        try:
            refined = apply_refinement(
                reel,
                float(entry["new_start_sec"]),
                float(entry["new_end_sec"]),
                analysis,
                config,
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("refinement entry for %s malformed: %s", reel.candidate_id, exc)
            refined = reel
        out.append(refined)
    return out
