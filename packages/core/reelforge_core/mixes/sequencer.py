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

MIX_PROMPT_VERSION = "m4"  # m4: long-form chapters, priorities, intro, re-hook
# Plain-text words sent per long-form section (timestamps would cost ~4x):
# up to SECTION_TEXT_WORDS_LONG each, cut at a sentence end, within a total
# budget so a 40-section pool stays a sane prompt.
SECTION_TEXT_WORDS = 240
SECTION_TEXT_WORDS_LONG = 400
LONG_TEXT_BUDGET_WORDS = 12000
# Long-form sequencing is the whole story's structure: the strongest model.
LONG_FORM_MODEL = "claude-opus-5-5"
INTRO_MAX_SEC = 30.0
INTRO_LINE_MIN_SEC = 1.5
INTRO_LINE_MAX_SEC = 8.0
INTRO_MAX_LINES = 4
SENTENCE_TRIM_MIN_SEC = 10.0
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
    "You will receive every candidate moment: a 5-frame contact sheet (2s "
    "BEFORE the moment / start / peak / end / 2s AFTER — the red-bordered "
    "outer frames are NOT part of the moment; black = past the footage edge) "
    "plus its data (which source video, bounds, transcript, energy, "
    "features).\n\n"
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

LONG_FORM_NOTE = (
    "\n\nLONG-FORM VIDEO\n"
    "The target is about {minutes:.0f} minutes, so the candidates are whole "
    "SECTIONS (roughly 20-90s), not highlights, and the result is one "
    "watchable long video rather than a reel. Keep the viewer oriented: an "
    "intro/overview belongs near the start, explanations keep setup before "
    "detail, and a wrap-up/outro goes last. Cover the substance of every "
    "source video unless it repeats another; interleave only where it helps "
    "the flow. Use as many sections as the target needs — for long-form, "
    "using a minority of the pool is NOT expected."
)

LONG_FORM_STRUCTURE = (
    "\n\nRETENTION STRUCTURE (long-form)\n"
    "- keep_priority (1-5) on every chosen section: 5 = the video fails "
    "without it, 1 = nice to have. If the video runs long, low-priority "
    "sections are shortened or cut first.\n"
    "- chapter_title on the section that STARTS each chapter: the first "
    "chosen section always starts one; aim for one chapter per 1-3 minutes, "
    "at least 3; titles are short, specific and promise what the chapter "
    "delivers (not 'Part 2').\n"
    "- intro_lines: 3-4 short, punchy lines (each 2-7s, together under 25s) "
    "cut from the chosen sections — absolute start_sec/end_sec inside that "
    "section, between words. They play FIRST as a cold-open montage, so the "
    "first one must state or confirm the title's promise and the set should "
    "make a viewer need the rest. Never a greeting.\n"
    "- rehook_text: a short 'coming up' teaser (<= 60 chars) for the second "
    "half, shown near the midpoint to stop mid-video drop-off."
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


RECORD_MIX_LONG: dict[str, Any] = json.loads(json.dumps(RECORD_MIX))
_ITEM = RECORD_MIX_LONG["input_schema"]["properties"]["sequence"]["items"]["properties"]
_ITEM["keep_priority"] = {"type": "integer", "minimum": 1, "maximum": 5}
_ITEM["chapter_title"] = {"type": ["string", "null"], "maxLength": 60}
RECORD_MIX_LONG["input_schema"]["properties"]["intro_lines"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "moment_id": {"type": "string"},
            "start_sec": {"type": "number"},
            "end_sec": {"type": "number"},
        },
        "required": ["moment_id", "start_sec", "end_sec"],
    },
}
RECORD_MIX_LONG["input_schema"]["properties"]["rehook_text"] = {"type": ["string", "null"], "maxLength": 60}
del _ITEM


@dataclass
class SequencedMix:
    shots: list[tuple[str, float, float]]  # (asset_id, in_ts, out_ts)
    title: str
    hook: str
    suggested_mood: str
    content_style: str
    reasons: list[str] = field(default_factory=list)
    fallback: bool = False
    # Long-form (CP11), aligned with `shots`: keep priority 1-5 and the
    # chapter title a shot starts (None = continues the current chapter).
    priorities: list[int] = field(default_factory=list)
    chapter_titles: list[str | None] = field(default_factory=list)
    intro: list[tuple[str, float, float]] = field(default_factory=list)
    rehook_text: str | None = None


def _sentence_text(words: list[str], limit: int) -> str:
    """At most `limit` words, cut back to the last sentence end when there
    is one past the halfway mark. Pure."""
    if len(words) <= limit:
        return " ".join(words)
    cut = words[:limit]
    for i in range(len(cut) - 1, limit // 2, -1):
        if cut[i].rstrip().endswith((".", "!", "?")):
            return " ".join(cut[: i + 1])
    return " ".join(cut) + " …"


def build_moment_context(
    moment: MinedMoment,
    analysis: AnalysisReport | None,
    asset_name: str,
    text_words: int = SECTION_TEXT_WORDS,
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
        # The opening words keep their timestamps (trims act at the edges).
        # Long-form sections (20s+) also carry what the WHOLE section says:
        # at most 80 words (~25s of speech) left the model ordering 20-90s
        # sections from their first sentence alone.
        "transcript_words": [[t, w] for t, w in words[:24]],
        **(
            {"transcript_text": _sentence_text([w for _, w in words], text_words)}
            if c.duration_sec >= 20 and len(words) > 24
            else {}
        ),
        "energy_peak_z": moment.features.energy_peak_z,
    }


def validate_sequence(
    raw: dict,
    pool: list[MinedMoment],
    target_sec: float,
    analyses: dict[str, AnalysisReport | None],
    long_form: bool = False,
) -> SequencedMix:
    """Coerce the model's answer into a safe sequence. Pure. Long-form also
    keeps priorities, chapter starts, the intro montage and the re-hook, and
    fits length by trimming sentences before dropping sections."""
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

    events_cache: dict[str, list] = {}

    def _events(aid: str) -> list:
        # Mined bounds were already moved off action events; a ±1s trim must
        # not re-cut one (reels/events.py).
        if aid not in events_cache:
            from reelforge_core.reels.events import detect_events

            a = analyses.get(aid)
            events_cache[aid] = detect_events(a) if a is not None else []
        return events_cache[aid]

    entries: list[tuple[MinedMoment, float, float]] = []
    seen: set[str] = set()
    reasons: list[str] = []
    meta: dict[str, tuple[int, str | None]] = {}  # moment_id -> (priority, chapter)

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
            evs = _events(m.asset_id)
            if evs:
                from reelforge_core.reels.events import edge_ok

                if not edge_ok(in_ts, "start", evs, dur_limit):
                    in_ts = m.candidate.start_sec
                if not edge_ok(out_ts, "end", evs, dur_limit):
                    out_ts = m.candidate.end_sec
            if out_ts - in_ts < MIN_SHOT_SEC:
                continue
            if _dup_of_kept(m.asset_id, in_ts, out_ts):
                continue
            seen.add(m.moment_id)
            entries.append((m, round(in_ts, 3), round(out_ts, 3)))
            prio = item.get("keep_priority")
            prio = int(prio) if isinstance(prio, (int, float)) and 1 <= prio <= 5 else 3
            chap = item.get("chapter_title")
            chap = str(chap).strip()[:60] if isinstance(chap, str) and chap.strip() else None
            meta[m.moment_id] = (prio, chap)
            if item.get("reason"):
                reasons.append(str(item["reason"])[:120])
        except (TypeError, ValueError):
            continue

    # Over-length: drop the weakest-prescore entries (keeping order and at
    # least MIN_SEQUENCE_LEN) until within the band.
    def _total(es):
        return sum(o - i for _, i, o in es)

    if long_form:
        entries = _fit_long_form(entries, meta, target_sec * (1 + TARGET_BAND), analyses)
    while _total(entries) > target_sec * (1 + TARGET_BAND) and len(entries) > MIN_SEQUENCE_LEN:
        if long_form:
            # Never the opening or the closing section; lowest priority first.
            middle = entries[1:-1] or entries
            weakest = min(middle, key=lambda e: (meta.get(e[0].moment_id, (3, None))[0], e[0].score))
        else:
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
    mix = SequencedMix(
        shots=[(m.asset_id, i, o) for m, i, o in entries],
        title=str(raw.get("title") or "Project mix")[:60],
        hook=str(raw.get("hook") or "")[:140],
        suggested_mood=mood,
        content_style=style,
        reasons=reasons,
    )
    if long_form:
        mix.priorities = [meta.get(m.moment_id, (3, None))[0] for m, _, _ in entries]
        mix.chapter_titles = [meta.get(m.moment_id, (3, None))[1] for m, _, _ in entries]
        mix.intro = validate_intro(raw.get("intro_lines"), by_id, analyses)
        rehook = raw.get("rehook_text")
        if isinstance(rehook, str):
            # The overlay adds "Coming up:" itself; the model often does too.
            import re as _re

            rehook = _re.sub(r"^\s*coming up\s*[:\-–—]?\s*", "", rehook, flags=_re.I).strip()
        mix.rehook_text = str(rehook)[:60] if isinstance(rehook, str) and rehook else None
    return mix


def _fit_long_form(
    entries: list[tuple[MinedMoment, float, float]],
    meta: dict[str, tuple[int, str | None]],
    limit: float,
    analyses: dict[str, AnalysisReport | None],
) -> list[tuple[MinedMoment, float, float]]:
    """Shorten before cutting: trim the lowest-priority sections back to an
    earlier sentence end (keeping >= SENTENCE_TRIM_MIN_SEC and half the
    section) until the total fits or nothing trims. Pure."""
    from reelforge_core.reels.generators.sentence import build_units

    units_cache: dict[str, list] = {}

    def units(aid: str) -> list:
        if aid not in units_cache:
            a = analyses.get(aid)
            units_cache[aid] = build_units(a.transcript) if a is not None else []
        return units_cache[aid]

    out = list(entries)
    guard = 0
    while sum(o - i for _, i, o in out) > limit and guard < 500:
        guard += 1
        best = None
        for idx, (m, i, o) in enumerate(out):
            keep = max(SENTENCE_TRIM_MIN_SEC, (o - i) * 0.5)
            ends = [u.end for u in units(m.asset_id) if i + keep <= u.end < o - 1.0]
            if not ends:
                continue
            prio = meta.get(m.moment_id, (3, None))[0]
            cand = (prio, -(o - i), idx, round(max(ends), 3))
            if best is None or cand < best:
                best = cand
        if best is None:
            break
        _, _, idx, new_out = best
        m, i, _o = out[idx]
        out[idx] = (m, i, new_out)
    return out


def validate_intro(
    raw: Any,
    by_id: dict[str, MinedMoment],
    analyses: dict[str, AnalysisReport | None],
) -> list[tuple[str, float, float]]:
    """The cold-open intro montage: up to INTRO_MAX_LINES lines, each inside
    its moment, 1.5-8s, snapped off words (or dropped), no two overlapping,
    together <= INTRO_MAX_SEC. Pure."""
    from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start

    out: list[tuple[str, float, float]] = []
    total = 0.0
    for item in raw if isinstance(raw, list) else []:
        try:
            m = by_id.get(str(item.get("moment_id")))
            if m is None:
                continue
            s, e = float(item["start_sec"]), float(item["end_sec"])
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        s = max(s, m.candidate.start_sec)
        e = min(e, m.candidate.end_sec)
        a = analyses.get(m.asset_id)
        words = flatten_words(a.transcript) if a is not None and a.transcript else []
        if words:
            if any(ws < s < we for ws, we in words):
                s = snap_start(s, words, 0.4)
            if any(ws < e < we for ws, we in words):
                e = snap_end(e, words, 0.4)
            if any(ws < s < we for ws, we in words) or any(ws < e < we for ws, we in words):
                continue
        if not INTRO_LINE_MIN_SEC <= e - s <= INTRO_LINE_MAX_SEC + 0.3:
            continue
        if total + (e - s) > INTRO_MAX_SEC:
            break
        if any(aid == m.asset_id and min(e, oe) - max(s, os_) > 0 for aid, os_, oe in out):
            continue
        out.append((m.asset_id, round(s, 3), round(e, 3)))
        total += e - s
        if len(out) >= INTRO_MAX_LINES:
            break
    return out if len(out) >= 2 else []


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

    from reelforge_core.mixes.mining import LONG_FORM_THRESHOLD_SEC

    long_form = target_sec > LONG_FORM_THRESHOLD_SEC
    system = MIX_SYSTEM_PROMPT
    if long_form:
        system += LONG_FORM_NOTE.format(minutes=target_sec / 60.0) + LONG_FORM_STRUCTURE
    text_words = (
        min(SECTION_TEXT_WORDS_LONG, max(80, LONG_TEXT_BUDGET_WORDS // max(1, len(pool))))
        if long_form
        else SECTION_TEXT_WORDS
    )
    if prompt:
        system += USER_DIRECTION_TEMPLATE.format(prompt=prompt)

    blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Target duration: {target_sec:.0f}s. "
                f"{len(pool)} candidate moments from "
                f"{len({m.asset_id for m in pool})} source videos follow, in "
                "balanced prescore order (a weak prior). Each: a 5-frame "
                "contact sheet (the red-bordered outer frames lie outside the "
                "moment), then its data."
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
            m, analyses.get(m.asset_id), asset_names.get(m.asset_id, m.asset_id[:8]),
            text_words=text_words,
        )
        blocks.append({"type": "text", "text": json.dumps(ctx)})

    try:
        resp = await _call_model(
            client,
            model=model,
            temperature=0.0,
            system_prompt=system,
            messages=[{"role": "user", "content": blocks}],
            tools=[RECORD_MIX_LONG if long_form else RECORD_MIX],
            tool_name="record_mix",
            max_tokens=16000 if long_form else 8000,
        )
        raw = _extract_mix(resp)
        usage = _accumulate_usage(resp)
        mix = validate_sequence(raw, pool, target_sec, analyses, long_form=long_form)
        return mix, usage
    except Exception as exc:
        log.warning("mix sequencing failed; using deterministic fallback: %s", exc)
        return fallback_sequence(pool, target_sec), UsageTotals()
