"""Rank all candidate reels in a single batched Claude call with forced tool-use.

Design principle: never call the API per candidate. One call sees every span so
the model can compare them globally. Only batch if > 80 candidates (rare).
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from reelforge_core.errors import RankingError
from reelforge_core.models import (
    MOOD_VALUES,
    AnalysisReport,
    LoudnessPoint,
    RankedReel,
    ReelCandidate,
    ReelScores,
    SelectionConfig,
    Transcript,
    UsageTotals,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 5
TRANSCRIPT_SLICE_CAP = 3000
LARGE_SET_THRESHOLD = 80

SYSTEM_PROMPT_V1 = (
    "You are a senior short-form video editor selecting moments from a longer "
    "piece of footage for a 30-60 second standalone reel (the kind that would "
    "work on TikTok, Instagram Reels, or YouTube Shorts).\n\n"
    "You will receive a list of candidate spans. Each span is a contiguous run "
    "of scenes from the source with per-scene summaries, mood tags, and "
    "transcript.\n\n"
    "Your job: score every candidate on four dimensions (0-100 each), invent a "
    "title and a one-line hook for each, choose a suggested mood for music "
    "matching, and briefly justify each score.\n\n"
    "Score meanings (be harsh — most raw candidates are mediocre):\n"
    "- narrative_coherence: does this span tell a self-contained story or make "
    "a complete point? A random 45 seconds of B-roll scores low. A clear "
    "setup-development-payoff scores high.\n"
    "- hook_strength: would someone scrolling past stop in the first 2 seconds? "
    "Strong opening visual, unexpected moment, or compelling question scores "
    "high. Slow fade-in on a talking head scores low.\n"
    "- emotional_payoff: does the span deliver an emotional or informational "
    "punch? Laughter, revelation, surprise, release of tension all score high. "
    "Flat monotone delivery without climax scores low.\n"
    "- standalone_clarity: does the span make sense without the surrounding "
    "video? Heavy reliance on prior context ('as I said earlier...') scores "
    "low. Self-contained scores high.\n\n"
    "Call the record_rankings tool exactly once with rankings for ALL "
    "candidates. Do not omit candidates. Do not include any text outside the "
    "tool call.\n\n"
    "Titles: <= 60 characters, no emoji, no trailing punctuation, written like "
    "a content creator would actually title a reel.\n"
    "Hooks: <= 140 characters, one sentence, designed to retain a viewer past "
    "3 seconds.\n"
    "Justifications: 1-2 sentences, specific to the content, not generic.\n"
    "Mood: pick from the fixed vocabulary to aid music selection downstream."
)

SYSTEM_PROMPT_V2 = (
    "You are a senior short-form video editor selecting moments from a longer "
    "piece of footage for a 30-60 second standalone reel (the kind that would "
    "work on TikTok, Instagram Reels, or YouTube Shorts).\n\n"
    "You will receive every viable candidate span at once. Each candidate "
    "comes as a 3-frame contact sheet (opening frame / energy peak / closing "
    "frame) followed by its data as JSON: per-scene summaries and tags, "
    "word-timestamped transcript, per-second energy, and the heuristic "
    "features that pre-selected it. Candidates arrive in heuristic prescore "
    "order — treat that order as a weak prior, not the answer.\n\n"
    "You are seeing the whole set: first decide the ORDERING — which would "
    "you post first, second, third. Record it as rank_position (1 = best). "
    "Then assign the four dimension scores so they reflect RELATIVE quality "
    "within this set, using the full 0-100 range; the top 5 candidates must "
    "be separated by at least 5 points of overall quality. Do not compress "
    "everything into a 55-80 band.\n\n"
    "Score meanings (be harsh — most raw candidates are mediocre):\n"
    "- narrative_coherence: does this span tell a self-contained story or make "
    "a complete point? A random 45 seconds of B-roll scores low. A clear "
    "setup-development-payoff scores high.\n"
    "- hook_strength: would someone scrolling past stop in the first 2 seconds? "
    "Judge from the FIRST contact-sheet frame and the opening line. Strong "
    "opening visual, unexpected moment, or compelling question scores high.\n"
    "- emotional_payoff: does the span deliver an emotional or informational "
    "punch? Laughter, revelation, surprise, release of tension all score high.\n"
    "- standalone_clarity: does the span make sense without the surrounding "
    "video? Heavy reliance on prior context scores low.\n\n"
    "opening_description: at most 80 characters stating what is LITERALLY on "
    "screen and said in the first 2 seconds — from the first contact-sheet "
    "frame and the opening line. No marketing language; describe, don't sell.\n\n"
    "Call the record_rankings tool exactly once with rankings for ALL "
    "candidates. Do not omit candidates. Do not include any text outside the "
    "tool call.\n\n"
    "Titles: <= 60 characters, no emoji, no trailing punctuation, written like "
    "a content creator would actually title a reel.\n"
    "Hooks: <= 140 characters, one sentence, designed to retain a viewer past "
    "3 seconds.\n"
    "Justifications: 1-2 sentences, specific to the content, not generic.\n"
    "Mood: pick from the fixed vocabulary to aid music selection downstream."
)

RECORD_RANKINGS: dict[str, Any] = {
    "name": "record_rankings",
    "description": "Record rankings for every candidate reel.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "title": {"type": "string", "maxLength": 60},
                        "hook": {"type": "string", "maxLength": 140},
                        "justification": {"type": "string", "maxLength": 300},
                        "suggested_mood": {
                            "type": "string",
                            "enum": list(MOOD_VALUES),
                        },
                        "scores": {
                            "type": "object",
                            "properties": {
                                "narrative_coherence": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 100,
                                },
                                "hook_strength": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 100,
                                },
                                "emotional_payoff": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 100,
                                },
                                "standalone_clarity": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 100,
                                },
                            },
                            "required": [
                                "narrative_coherence",
                                "hook_strength",
                                "emotional_payoff",
                                "standalone_clarity",
                            ],
                        },
                    },
                    "required": [
                        "candidate_id",
                        "title",
                        "hook",
                        "justification",
                        "suggested_mood",
                        "scores",
                    ],
                },
            }
        },
        "required": ["rankings"],
    },
}


# v2 tool: v1 fields plus the listwise ordering + literal opening description.
RECORD_RANKINGS_V2: dict[str, Any] = copy.deepcopy(RECORD_RANKINGS)
_V2_ITEM = RECORD_RANKINGS_V2["input_schema"]["properties"]["rankings"]["items"]
_V2_ITEM["properties"]["rank_position"] = {
    "type": "integer",
    "minimum": 1,
    "description": "Explicit listwise order: 1 = the candidate you would post first.",
}
_V2_ITEM["properties"]["opening_description"] = {
    "type": "string",
    "maxLength": 80,
    "description": "What is literally on screen and said in the first 2 seconds.",
}
_V2_ITEM["required"].extend(["rank_position", "opening_description"])
del _V2_ITEM


# When a user prompt is active, candidates scoring below this floor on
# prompt_relevance are dropped entirely (strict filter). The rubric anchors
# <=20 as "unrelated" and ~50 as "partial", so 35 removes clear misses while
# keeping partial matches in play.
PROMPT_RELEVANCE_FLOOR = 35

USER_DIRECTION_TEMPLATE = (
    "\n\nUSER DIRECTION\n"
    "The user gave this instruction for what they want in the selected reels:\n"
    '"{prompt}"\n\n'
    "Apply it in two ways:\n"
    "1. Content matching: score each candidate on a fifth dimension, "
    "prompt_relevance (0-100): how well the candidate's actual content — "
    "scene summaries, tags, transcript — matches the instruction. "
    "90+ = the requested content is clearly the focus of the span. "
    "Around 50 = partially or indirectly related. 20 or below = unrelated. "
    "Judge only from the evidence provided; never invent content. "
    "If the instruction is purely about style or feel rather than content, "
    "score prompt_relevance on how well the span could carry that feel.\n"
    "2. Style: if the instruction expresses a desired feel, tone, or energy "
    "(e.g. 'make it feel intense'), reflect it in suggested_mood (still from "
    "the fixed vocabulary) and write the title and hook in that tone.\n"
    "Keep the four original dimensions at their normal meanings — do not "
    "inflate them for relevant candidates; prompt_relevance is reported "
    "separately."
)


def build_system_prompt(config: SelectionConfig) -> str:
    """SYSTEM_PROMPT_V2, plus the user-direction block when a prompt is set.

    SYSTEM_PROMPT_V1 stays in the file for reference/stamp archaeology only —
    old stamps recording ranking_prompt_version "v1" simply won't match and
    force one fresh rank."""
    if not config.prompt:
        return SYSTEM_PROMPT_V2
    return SYSTEM_PROMPT_V2 + USER_DIRECTION_TEMPLATE.format(prompt=config.prompt)


def build_ranking_tool(config: SelectionConfig) -> dict[str, Any]:
    """RECORD_RANKINGS_V2, plus a required prompt_relevance field when a prompt is set."""
    if not config.prompt:
        return RECORD_RANKINGS_V2
    tool = copy.deepcopy(RECORD_RANKINGS_V2)
    item = tool["input_schema"]["properties"]["rankings"]["items"]
    item["properties"]["prompt_relevance"] = {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
    }
    item["required"].append("prompt_relevance")
    return tool


@dataclass
class RankingResult:
    reels: list[RankedReel]
    usage: UsageTotals
    raw_rankings: list[dict]  # the unmodified tool input, merged across any retries


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _slice_transcript(
    transcript: Transcript | None, start: float, end: float
) -> str:
    """Word-granular slice: a word belongs to the span iff its midpoint does.

    Segment-granular slicing bled a whole segment's text into the span when it
    overlapped by as little as 0.1s; boundary words are the difference between
    "opens mid-sentence" and "opens on the hook" for the ranker.
    """
    if transcript is None:
        return ""
    parts: list[str] = []
    for seg in transcript.segments:
        if seg.end < start or seg.start > end:
            continue
        if seg.words:
            for w in seg.words:
                if start <= (w.start + w.end) / 2.0 <= end:
                    parts.append(w.word.strip())
        else:
            # No word timings (older artifacts / overrides): whole-segment fallback.
            parts.append(seg.text.strip())
    text = " ".join(parts).strip()
    if len(text) > TRANSCRIPT_SLICE_CAP:
        text = text[: TRANSCRIPT_SLICE_CAP - len("…[truncated]")] + "…[truncated]"
    return text


# transcript_words keeps this many words from each end of the span (with a
# "…" marker between when truncated) — openings and closings decide reels.
WORD_WINDOW = 60


def _span_words(
    transcript: Transcript | None, start: float, end: float
) -> list[tuple[float, str]]:
    """(start_time, word) for every word whose midpoint falls in the span."""
    if transcript is None:
        return []
    out: list[tuple[float, str]] = []
    for seg in transcript.segments:
        if seg.end < start or seg.start > end:
            continue
        for w in seg.words:
            if start <= (w.start + w.end) / 2.0 <= end:
                out.append((round(w.start, 2), w.word.strip()))
    return out


def build_candidate_context(
    candidate: ReelCandidate,
    analysis: AnalysisReport,
    *,
    features: Any | None = None,
    units: list | None = None,
    energy_z: list[tuple[float, float]] | None = None,
) -> dict:
    """The per-candidate JSON the ranker sees (v2).

    `features` is the candidate's PrescoreFeatures, `units` the asset's
    utterance units, `energy_z` the asset-wide (time, combined z) series —
    callers compute each once and share across candidates.
    """
    scenes = [analysis.scenes[i] for i in candidate.scene_indices]
    # Semantics is keyed by scene_index, which equals the list position; look up safely.
    sem_by_index = {s.scene_index: s for s in analysis.semantics}
    start, end = candidate.start_sec, candidate.end_sec

    scene_dicts: list[dict] = []
    for s in scenes:
        sem = sem_by_index.get(s.index)
        scene_dicts.append(
            {
                "index": s.index,
                "duration_sec": round(s.end_sec - s.start_sec, 2),
                "summary": sem.summary if sem else "",
                "mood": sem.mood if sem else "neutral",
                "tags": sem.tags if sem else [],
                "visual_energy": sem.visual_energy if sem else "medium",
                "has_speech": sem.has_speech if sem else False,
            }
        )

    words = _span_words(analysis.transcript, start, end)
    transcript_words: list[Any] = (
        [[t, w] for t, w in words]
        if len(words) <= 2 * WORD_WINDOW
        else [[t, w] for t, w in words[:WORD_WINDOW]]
        + ["…"]
        + [[t, w] for t, w in words[-WORD_WINDOW:]]
    )

    opening_line = ""
    closing_line = ""
    if units:
        in_span = [u for u in units if u.end > start and u.start < end]
        if in_span:
            opening_line = in_span[0].text[:200]
            closing_line = in_span[-1].text[:200]

    energy_series: list[float] = []
    if energy_z:
        energy_series = [round(z, 1) for t, z in energy_z if start <= t <= end]

    return {
        "candidate_id": candidate.candidate_id,
        "start_sec": round(start, 2),
        "end_sec": round(end, 2),
        "duration_sec": round(candidate.duration_sec, 2),
        "scene_count": candidate.scene_count,
        "source": candidate.source,
        "scenes": scene_dicts,
        "transcript_words": transcript_words,
        "opening_line": opening_line,
        "closing_line": closing_line,
        "energy_series": energy_series,
        "prescore_features": features.to_dict() if features is not None else None,
    }


# ---------------------------------------------------------------------------
# API call + retry
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    try:
        import anthropic  # type: ignore
    except Exception:  # pragma: no cover
        return False
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500
    return False


def _extract_rankings(resp: Any) -> list[dict]:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            data = block.input
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict) or "rankings" not in data:
                raise RankingError("tool_use input missing 'rankings' key")
            rankings = data["rankings"]
            if not isinstance(rankings, list):
                raise RankingError("tool_use 'rankings' is not a list")
            return rankings
    raise RankingError("model did not emit a tool_use block")


async def _call_model(
    client: Any,
    *,
    model: str,
    temperature: float,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_name: str = "record_rankings",
    max_tokens: int = 16000,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            # NOTE: anthropic SDK 1.0.0 removed the `temperature` kwarg from
            # messages.create, but the API still accepts it for
            # claude-sonnet-4-5 (verified 2026-08-27), so we pass it through
            # extra_body for reproducible ranking. If the ranking model ever
            # moves to a Claude 5 family model that rejects sampling params
            # (400), drop the extra_body line.
            resp = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                tools=tools if tools is not None else [RECORD_RANKINGS],
                tool_choice={"type": "tool", "name": tool_name},
                messages=messages,
                extra_body={"temperature": temperature},
            )
            stop_reason = getattr(resp, "stop_reason", None)
            if stop_reason not in {"tool_use", None}:
                log.warning(
                    "unexpected stop_reason=%s for ranking call; retrying", stop_reason
                )
                last_exc = RankingError(f"stop_reason={stop_reason}")
                await asyncio.sleep(min(60, 2**attempt + random.random()))
                continue
            return resp
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise RankingError(f"non-retryable Anthropic error: {exc}") from exc
            delay = min(60, 2**attempt + random.random())
            log.warning(
                "ranking call retry %d/%d in %.2fs due to %s",
                attempt + 1,
                MAX_RETRIES,
                delay,
                exc.__class__.__name__,
            )
            await asyncio.sleep(delay)
    raise RankingError(
        f"ranking call failed after {MAX_RETRIES} retries: {last_exc}"
    ) from last_exc


def _coerce_rankings(
    rankings: Iterable[dict],
    *,
    candidate_map: dict[str, ReelCandidate],
    prompt_active: bool = False,
) -> list[RankedReel]:
    """Validate + convert raw tool-input entries to RankedReel. Skips extras."""
    seen: set[str] = set()
    out: list[RankedReel] = []
    for entry in rankings:
        cid = entry.get("candidate_id")
        if cid in seen:
            log.warning("duplicate candidate_id in rankings: %s", cid)
            continue
        if cid not in candidate_map:
            log.warning("ranking for unknown candidate_id %s ignored", cid)
            continue
        try:
            scores = ReelScores(**entry["scores"])
            relevance: int | None = None
            if prompt_active:
                relevance = int(entry["prompt_relevance"])  # KeyError -> drop + retry
                overall = round(0.45 * relevance + 0.55 * scores.weighted, 2)
            else:
                overall = round(scores.weighted, 2)
            candidate = candidate_map[cid]
            rank_position = entry.get("rank_position")
            rank_position = int(rank_position) if rank_position is not None else None
            opening = entry.get("opening_description")
            opening = str(opening)[:80] if opening else None
            reel = RankedReel(
                candidate_id=cid,
                scene_indices=candidate.scene_indices,
                start_sec=candidate.start_sec,
                end_sec=candidate.end_sec,
                duration_sec=candidate.duration_sec,
                title=entry["title"],
                hook=entry["hook"],
                justification=entry["justification"],
                scores=scores,
                overall=overall,
                rank=0,  # assigned after dedup
                suggested_mood=entry["suggested_mood"],
                prompt_relevance=relevance,
                source=candidate.source,
                rank_position=rank_position,
                opening_description=opening,
            )
        except Exception as exc:
            log.warning(
                "ranking entry for %s failed validation and was dropped: %s",
                cid,
                exc,
            )
            continue
        seen.add(cid)
        out.append(reel)
    # Sort so the model's explicit listwise order breaks overall-score ties
    # (dedup's stable overall-desc sort then preserves this ordering).
    out.sort(key=lambda r: (-r.overall, r.rank_position if r.rank_position is not None else 1 << 30))
    return out


async def rank(
    candidates: list[ReelCandidate],
    analysis: AnalysisReport,
    config: SelectionConfig,
    *,
    client: Any | None = None,
    features: dict[str, Any] | None = None,
    sheets: dict[str, Path] | None = None,
) -> RankingResult:
    """Single listwise ranking call on the (shortlisted) candidates.

    `features` maps candidate_id -> PrescoreFeatures; `sheets` maps
    candidate_id -> contact-sheet JPEG path. Both are optional — the call
    degrades to text-only context without them. Retries on missing
    candidates (once)."""
    if not candidates:
        return RankingResult(reels=[], usage=UsageTotals(), raw_rankings=[])

    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()

    candidate_map = {c.candidate_id: c for c in candidates}

    if len(candidates) > LARGE_SET_THRESHOLD:
        # The pipeline's prescore shortlist (SelectionConfig.shortlist_size)
        # should keep sets far below this; if something bypasses it, rank the
        # first LARGE_SET_THRESHOLD in the given (prescore) order. The old
        # overlapping-batch path is gone — first-seen-wins merging of batches
        # scored on different scales was never sound.
        log.warning(
            "ranking set of %d exceeds %d — ranking only the first %d "
            "(raise SelectionConfig.shortlist_size deliberately if you want more)",
            len(candidates),
            LARGE_SET_THRESHOLD,
            LARGE_SET_THRESHOLD,
        )
        candidates = candidates[:LARGE_SET_THRESHOLD]
        candidate_map = {c.candidate_id: c for c in candidates}

    return await _rank_once(client, candidates, analysis, config, candidate_map, features=features, sheets=sheets)


async def _rank_once(
    client: Any,
    batch: list[ReelCandidate],
    analysis: AnalysisReport,
    config: SelectionConfig,
    candidate_map: dict[str, ReelCandidate],
    features: dict[str, Any] | None = None,
    sheets: dict[str, Path] | None = None,
) -> RankingResult:
    # Shared context computed once per call, not per candidate.
    from reelforge_core.reels.generators.moment import combined_scores
    from reelforge_core.reels.generators.sentence import build_units

    units = build_units(analysis.transcript)
    energy_z = combined_scores(analysis)

    blocks: list[dict] = [
        {
            "type": "text",
            "text": (
                "Candidates follow in heuristic prescore order (a weak prior, "
                "not the answer). Each candidate: a 3-frame contact sheet "
                "(opening / energy peak / closing frame), then its data as JSON."
            ),
        }
    ]
    for c in batch:
        sheet = sheets.get(c.candidate_id) if sheets else None
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
                log.warning("contact sheet unreadable for %s; sending text only", c.candidate_id)
        ctx = build_candidate_context(
            c,
            analysis,
            features=(features or {}).get(c.candidate_id),
            units=units,
            energy_z=energy_z,
        )
        blocks.append({"type": "text", "text": json.dumps(ctx, indent=2)})
    messages: list[dict] = [{"role": "user", "content": blocks}]
    system_prompt = build_system_prompt(config)
    tools = [build_ranking_tool(config)]
    prompt_active = bool(config.prompt)

    resp = await _call_model(
        client,
        model=config.ranking_model,
        temperature=config.temperature,
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
    )
    rankings_raw = _extract_rankings(resp)
    usage = _accumulate_usage(resp)

    ranked = _coerce_rankings(rankings_raw, candidate_map=candidate_map, prompt_active=prompt_active)
    missing = [c.candidate_id for c in batch if c.candidate_id not in {r.candidate_id for r in ranked}]

    # One targeted retry for missing candidates.
    if missing:
        log.warning(
            "ranking response missing %d candidates; issuing one corrective retry",
            len(missing),
        )
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "retry0", "name": "record_rankings", "input": {"rankings": rankings_raw}}],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "retry0",
                        "content": (
                            "You did not rank the following candidate_ids: "
                            + ", ".join(missing)
                            + ". Call record_rankings again with rankings for ALL candidates including these."
                        ),
                    }
                ],
            }
        )
        try:
            resp2 = await _call_model(
                client,
                model=config.ranking_model,
                temperature=config.temperature,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
            )
            rankings2 = _extract_rankings(resp2)
            usage2 = _accumulate_usage(resp2)
            usage.input_tokens += usage2.input_tokens
            usage.output_tokens += usage2.output_tokens
            ranked2 = _coerce_rankings(rankings2, candidate_map=candidate_map, prompt_active=prompt_active)
            # Merge: prefer the second response's entry when it covers a candidate
            by_id = {r.candidate_id: r for r in ranked}
            for r in ranked2:
                by_id[r.candidate_id] = r
            ranked = list(by_id.values())
            rankings_raw = list(rankings_raw) + [
                r for r in rankings2 if r.get("candidate_id") in missing
            ]
            still_missing = [
                c.candidate_id for c in batch if c.candidate_id not in by_id
            ]
            if still_missing:
                log.warning(
                    "after retry, %d candidates still unranked and will be dropped: %s",
                    len(still_missing),
                    still_missing,
                )
        except RankingError as exc:
            log.warning("retry for missing candidates failed: %s", exc)

    return RankingResult(reels=ranked, usage=usage, raw_rankings=rankings_raw)


def _accumulate_usage(resp: Any) -> UsageTotals:
    u = UsageTotals()
    usage = getattr(resp, "usage", None)
    if usage is not None:
        u.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        u.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
    return u
