"""The mix sequencing call: one AI pass ordering moments across clips.

Input: the pooled moments (CP0) with contact sheets. Output: an ordered
sequence (with small optional trims), plus title/hook/mood/content_style for
the mix. Everything the model returns passes through pure
`validate_sequence` — unknown ids drop, trims clamp and speech-snap, the
total duration is coerced toward the target — and any failure falls back to
a deterministic round-robin sequence, so a mix job always has a timeline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reelforge_core.mixes.mining import MinedMoment
from reelforge_core.models import MOOD_VALUES, AnalysisReport, UsageTotals

log = logging.getLogger(__name__)

MIX_PROMPT_VERSION = "m1"
TRIM_MAX_SEC = 1.0
MIN_SHOT_SEC = 0.5
MIN_SEQUENCE_LEN = 3
# Accept totals within this band of the target; outside it we drop the
# weakest entries / top up from the unused pool.
TARGET_BAND = 0.20
# The model sometimes picks several distinct moment_ids covering the same
# stretch of one clip (live-verified 2026-09-01: three 8s windows over the
# same 14s region) — the render then replays near-identical footage. Reject
# any span whose overlap with an already-kept same-asset span exceeds this
# (intersection / shorter duration).
SAME_ASSET_OVERLAP_MAX = 0.5

STYLE_ENUM = ("classic", "hype", "talking_head", "cinematic", "chill")

MIX_SYSTEM_PROMPT = (
    "You are a senior short-form editor building ONE reel from highlight "
    "moments mined across SEVERAL source videos of the same project.\n\n"
    "You will receive every candidate moment: a 3-frame contact sheet plus "
    "its data (which source video, bounds, transcript, energy, features).\n\n"
    "Sequence a reel with a real arc: open on the strongest hook, build "
    "variety and momentum, land on the payoff. Interleave source videos "
    "when it improves variety or continuity — do not simply play each video "
    "in order. Skip weak or near-duplicate moments; using a minority of the "
    "pool is normal. Aim for the target duration within about 15%.\n\n"
    "Per chosen moment you may trim up to 1.0s off either edge "
    "(trim_start_sec/trim_end_sec, positive = tighter).\n\n"
    "Also name the mix: title (<=60 chars, like a creator would), hook "
    "(<=140), suggested_mood (fixed vocabulary, drives music), and "
    "content_style — the editing grammar that suits the WHOLE mix "
    "(hype = fast beat cuts; talking_head = jump cuts + captions; "
    "cinematic = long dissolves; chill = gentle fades; classic = "
    "conservative).\n\n"
    "Call record_mix exactly once."
)

USER_DIRECTION_TEMPLATE = (
    "\n\nUSER DIRECTION\n"
    'The user asked for: "{prompt}"\n'
    "Choose moments that match it; if it names a feel or editing style, set "
    "suggested_mood and content_style accordingly — the user's wording wins."
)

RECORD_MIX: dict[str, Any] = {
    "name": "record_mix",
    "description": "Record the sequenced mix.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sequence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "moment_id": {"type": "string"},
                        "trim_start_sec": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                        "trim_end_sec": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                        "reason": {"type": "string", "maxLength": 120},
                    },
                    "required": ["moment_id"],
                },
            },
            "title": {"type": "string", "maxLength": 60},
            "hook": {"type": "string", "maxLength": 140},
            "suggested_mood": {"type": "string", "enum": list(MOOD_VALUES)},
            "content_style": {"type": "string", "enum": list(STYLE_ENUM)},
            "reason": {"type": "string", "maxLength": 300},
        },
        "required": ["sequence", "title", "hook", "suggested_mood", "content_style"],
    },
}


@dataclass
class SequencedMix:
    shots: list[tuple[str, float, float]]  # (asset_id, in_ts, out_ts)
    title: str
    hook: str
    suggested_mood: str
    content_style: str
    reasons: list[str] = field(default_factory=list)
    fallback: bool = False


def build_moment_context(
    moment: MinedMoment,
    analysis: AnalysisReport | None,
    asset_name: str,
) -> dict:
    """The JSON block the sequencer sees for one moment. Pure."""
    from reelforge_core.reels.rank import _span_words

    c = moment.candidate
    summary = ""
    tags: list[str] = []
    if analysis is not None:
        sem_by_idx = {s.scene_index: s for s in analysis.semantics}
        for idx in c.scene_indices:
            sem = sem_by_idx.get(idx)
            if sem is not None:
                summary = summary or sem.summary
                tags.extend(t for t in sem.tags if t not in tags)
    words = (
        _span_words(analysis.transcript, c.start_sec, c.end_sec)
        if analysis is not None
        else []
    )
    return {
        "moment_id": moment.moment_id,
        "source_video": asset_name,
        "start_sec": round(c.start_sec, 2),
        "end_sec": round(c.end_sec, 2),
        "duration_sec": round(c.duration_sec, 2),
        "generator": c.source,
        "prescore": moment.score,
        "features": moment.features.to_dict(),
        "scene_summary": summary,
        "tags": tags[:7],
        "transcript_words": [[t, w] for t, w in words[:24]],
        "energy_peak_z": moment.features.energy_peak_z,
    }


def validate_sequence(
    raw: dict,
    pool: list[MinedMoment],
    target_sec: float,
    analyses: dict[str, AnalysisReport | None],
) -> SequencedMix:
    """Coerce the model's answer into a safe sequence. Pure."""
    from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start

    by_id = {m.moment_id: m for m in pool}
    words_cache: dict[str, list[tuple[float, float]]] = {}

    def _words(aid: str) -> list[tuple[float, float]]:
        if aid not in words_cache:
            a = analyses.get(aid)
            words_cache[aid] = (
                flatten_words(a.transcript) if a is not None and a.transcript else []
            )
        return words_cache[aid]

    entries: list[tuple[MinedMoment, float, float]] = []
    seen: set[str] = set()
    reasons: list[str] = []

    def _dup_of_kept(aid: str, in_ts: float, out_ts: float) -> bool:
        for kept, ki, ko in entries:
            if kept.asset_id != aid:
                continue
            inter = min(out_ts, ko) - max(in_ts, ki)
            shorter = min(out_ts - in_ts, ko - ki)
            if shorter > 0 and inter / shorter > SAME_ASSET_OVERLAP_MAX:
                return True
        return False

    for item in raw.get("sequence", []) or []:
        try:
            m = by_id.get(str(item.get("moment_id")))
            if m is None or m.moment_id in seen:
                continue
            a = analyses.get(m.asset_id)
            dur_limit = a.duration if a is not None else m.candidate.end_sec
            t0 = min(max(float(item.get("trim_start_sec") or 0.0), -TRIM_MAX_SEC), TRIM_MAX_SEC)
            t1 = min(max(float(item.get("trim_end_sec") or 0.0), -TRIM_MAX_SEC), TRIM_MAX_SEC)
            in_ts = max(0.0, m.candidate.start_sec + t0)
            out_ts = min(dur_limit, m.candidate.end_sec - t1)
            w = _words(m.asset_id)
            if w:
                if any(ws < in_ts < we for ws, we in w):
                    in_ts = max(0.0, snap_start(in_ts, w, 0.6))
                if any(ws < out_ts < we for ws, we in w):
                    out_ts = min(dur_limit, snap_end(out_ts, w, 0.6))
            if out_ts - in_ts < MIN_SHOT_SEC:
                continue
            if _dup_of_kept(m.asset_id, in_ts, out_ts):
                continue
            seen.add(m.moment_id)
            entries.append((m, round(in_ts, 3), round(out_ts, 3)))
            if item.get("reason"):
                reasons.append(str(item["reason"])[:120])
        except (TypeError, ValueError):
            continue

    # Over-length: drop the weakest-prescore entries (keeping order and at
    # least MIN_SEQUENCE_LEN) until within the band.
    def _total(es):
        return sum(o - i for _, i, o in es)

    while _total(entries) > target_sec * (1 + TARGET_BAND) and len(entries) > MIN_SEQUENCE_LEN:
        weakest = min(entries, key=lambda e: e[0].score)
        entries.remove(weakest)

    # Under-length: top up with the best unused pool moments, inserted just
    # before the final shot so the model's chosen payoff stays last.
    unused = sorted(
        (m for m in pool if m.moment_id not in seen),
        key=lambda m: -m.score,
    )
    for m in unused:
        if _total(entries) >= target_sec * (1 - TARGET_BAND):
            break
        if _dup_of_kept(m.asset_id, m.candidate.start_sec, m.candidate.end_sec):
            continue
        entry = (m, m.candidate.start_sec, m.candidate.end_sec)
        if len(entries) >= 1:
            entries.insert(len(entries) - 1, entry)
        else:
            entries.append(entry)
        seen.add(m.moment_id)

    if len(entries) < MIN_SEQUENCE_LEN:
        raise ValueError(
            f"sequence unusable: {len(entries)} valid entr(ies) after validation"
        )

    mood = raw.get("suggested_mood")
    if mood not in MOOD_VALUES:
        mood = "neutral"
    style = raw.get("content_style")
    if style not in STYLE_ENUM:
        style = "classic"
    return SequencedMix(
        shots=[(m.asset_id, i, o) for m, i, o in entries],
        title=str(raw.get("title") or "Project mix")[:60],
        hook=str(raw.get("hook") or "")[:140],
        suggested_mood=mood,
        content_style=style,
        reasons=reasons,
    )


def fallback_sequence(pool: list[MinedMoment], target_sec: float) -> SequencedMix:
    """Deterministic no-AI sequence: the balanced pool order (already
    round-robin across assets, best-first) up to the target duration."""
    shots: list[tuple[str, float, float]] = []
    total = 0.0
    for m in pool:
        if total >= target_sec:
            break
        span = (m.asset_id, m.candidate.start_sec, m.candidate.end_sec)
        dup = False
        for aid, ki, ko in shots:
            if aid != span[0]:
                continue
            inter = min(span[2], ko) - max(span[1], ki)
            shorter = min(span[2] - span[1], ko - ki)
            if shorter > 0 and inter / shorter > SAME_ASSET_OVERLAP_MAX:
                dup = True
                break
        if dup:
            continue
        shots.append(span)
        total += m.candidate.duration_sec
    return SequencedMix(
        shots=shots,
        title="Project mix",
        hook="",
        suggested_mood="neutral",
        content_style="classic",
        fallback=True,
    )


def _extract_mix(resp: Any) -> dict:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            inp = getattr(block, "input", None)
            if isinstance(inp, str):
                inp = json.loads(inp)
            if isinstance(inp, dict) and "sequence" in inp:
                return inp
    raise ValueError("no record_mix tool_use block in response")


async def sequence_mix(
    pool: list[MinedMoment],
    analyses: dict[str, AnalysisReport | None],
    asset_names: dict[str, str],
    *,
    target_sec: float,
    prompt: str | None = None,
    model: str,
    sheets: dict[str, Path] | None = None,
    client: Any | None = None,
) -> tuple[SequencedMix, UsageTotals]:
    """One sequencing call; falls back to the deterministic sequence on any
    failure. Never raises."""
    import base64

    from reelforge_core.reels.rank import _accumulate_usage, _call_model

    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()

    system = MIX_SYSTEM_PROMPT
    if prompt:
        system += USER_DIRECTION_TEMPLATE.format(prompt=prompt)

    blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Target duration: {target_sec:.0f}s. "
                f"{len(pool)} candidate moments from "
                f"{len({m.asset_id for m in pool})} source videos follow, in "
                "balanced prescore order (a weak prior). Each: contact sheet "
                "(start / peak / end frames), then its data."
            ),
        }
    ]
    for m in pool:
        sheet = (sheets or {}).get(m.moment_id)
        if sheet is not None:
            try:
                data = base64.standard_b64encode(Path(sheet).read_bytes()).decode()
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": data,
                        },
                    }
                )
            except OSError:
                pass
        ctx = build_moment_context(
            m, analyses.get(m.asset_id), asset_names.get(m.asset_id, m.asset_id[:8])
        )
        blocks.append({"type": "text", "text": json.dumps(ctx)})

    try:
        resp = await _call_model(
            client,
            model=model,
            temperature=0.0,
            system_prompt=system,
            messages=[{"role": "user", "content": blocks}],
            tools=[RECORD_MIX],
            tool_name="record_mix",
            max_tokens=8000,
        )
        raw = _extract_mix(resp)
        usage = _accumulate_usage(resp)
        mix = validate_sequence(raw, pool, target_sec, analyses)
        return mix, usage
    except Exception as exc:
        log.warning("mix sequencing failed; using deterministic fallback: %s", exc)
        return fallback_sequence(pool, target_sec), UsageTotals()
