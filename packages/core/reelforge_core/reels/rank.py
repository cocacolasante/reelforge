"""Rank all candidate reels in a single batched Claude call with forced tool-use.

Design principle: never call the API per candidate. One call sees every span so
the model can compare them globally. Only batch if > 80 candidates (rare).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
from dataclasses import dataclass
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
BATCH_SIZE = 60
BATCH_OVERLAP = 10

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
    if transcript is None:
        return ""
    parts: list[str] = []
    for seg in transcript.segments:
        if seg.end < start or seg.start > end:
            continue
        parts.append(seg.text.strip())
    text = " ".join(parts).strip()
    if len(text) > TRANSCRIPT_SLICE_CAP:
        text = text[: TRANSCRIPT_SLICE_CAP - len("…[truncated]")] + "…[truncated]"
    return text


def _slice_loudness(
    loudness: list[LoudnessPoint], start: float, end: float
) -> list[float]:
    return [p.lufs for p in loudness if start <= p.time_sec <= end]


def build_candidate_context(
    candidate: ReelCandidate, analysis: AnalysisReport
) -> dict:
    scenes = [analysis.scenes[i] for i in candidate.scene_indices]
    # Semantics is keyed by scene_index, which equals the list position; look up safely.
    sem_by_index = {s.scene_index: s for s in analysis.semantics}
    transcript_slice = _slice_transcript(
        analysis.transcript, candidate.start_sec, candidate.end_sec
    )
    loudness_slice = _slice_loudness(
        analysis.loudness, candidate.start_sec, candidate.end_sec
    )

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

    stats: dict[str, float | None] = {
        "mean_lufs": None,
        "peak_lufs": None,
        "dynamic_range_lu": None,
    }
    if loudness_slice:
        stats["mean_lufs"] = round(statistics.fmean(loudness_slice), 1)
        stats["peak_lufs"] = round(max(loudness_slice), 1)
        if len(loudness_slice) > 1:
            stats["dynamic_range_lu"] = round(
                max(loudness_slice) - min(loudness_slice), 1
            )

    return {
        "candidate_id": candidate.candidate_id,
        "start_sec": round(candidate.start_sec, 2),
        "end_sec": round(candidate.end_sec, 2),
        "duration_sec": round(candidate.duration_sec, 2),
        "scene_count": candidate.scene_count,
        "scenes": scene_dicts,
        "transcript": transcript_slice,
        "loudness_stats": stats,
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
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=8000,
                temperature=temperature,
                system=system_prompt,
                tools=[RECORD_RANKINGS],
                tool_choice={"type": "tool", "name": "record_rankings"},
                messages=messages,
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
            candidate = candidate_map[cid]
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
                overall=round(scores.weighted, 2),
                rank=0,  # assigned after dedup
                suggested_mood=entry["suggested_mood"],
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
    return out


async def rank(
    candidates: list[ReelCandidate],
    analysis: AnalysisReport,
    config: SelectionConfig,
    *,
    client: Any | None = None,
) -> RankingResult:
    """Single batched ranking call. Retries on missing candidates (once)."""
    if not candidates:
        return RankingResult(reels=[], usage=UsageTotals(), raw_rankings=[])

    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()

    candidate_map = {c.candidate_id: c for c in candidates}

    if len(candidates) > LARGE_SET_THRESHOLD:
        log.info(
            "large candidate set (%d) — splitting into overlapping batches of %d",
            len(candidates),
            BATCH_SIZE,
        )
        merged = await _rank_batched(client, candidates, analysis, config)
        return merged

    return await _rank_once(client, candidates, analysis, config, candidate_map)


async def _rank_once(
    client: Any,
    batch: list[ReelCandidate],
    analysis: AnalysisReport,
    config: SelectionConfig,
    candidate_map: dict[str, ReelCandidate],
) -> RankingResult:
    contexts = [build_candidate_context(c, analysis) for c in batch]
    user_payload = json.dumps({"candidates": contexts}, indent=2)
    messages: list[dict] = [{"role": "user", "content": user_payload}]

    resp = await _call_model(
        client,
        model=config.ranking_model,
        temperature=config.temperature,
        system_prompt=SYSTEM_PROMPT_V1,
        messages=messages,
    )
    rankings_raw = _extract_rankings(resp)
    usage = _accumulate_usage(resp)

    ranked = _coerce_rankings(rankings_raw, candidate_map=candidate_map)
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
                system_prompt=SYSTEM_PROMPT_V1,
                messages=messages,
            )
            rankings2 = _extract_rankings(resp2)
            usage2 = _accumulate_usage(resp2)
            usage.input_tokens += usage2.input_tokens
            usage.output_tokens += usage2.output_tokens
            ranked2 = _coerce_rankings(rankings2, candidate_map=candidate_map)
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


async def _rank_batched(
    client: Any,
    candidates: list[ReelCandidate],
    analysis: AnalysisReport,
    config: SelectionConfig,
) -> RankingResult:
    """Rank a large candidate set in overlapping batches. Later batches use the
    same prompt; we merge results by candidate_id (first seen wins)."""
    seen_ids: set[str] = set()
    all_reels: list[RankedReel] = []
    usage = UsageTotals()
    all_raw: list[dict] = []

    step = BATCH_SIZE - BATCH_OVERLAP
    for start in range(0, len(candidates), step):
        batch = candidates[start : start + BATCH_SIZE]
        if not batch:
            break
        candidate_map = {c.candidate_id: c for c in batch}
        result = await _rank_once(client, batch, analysis, config, candidate_map)
        for reel in result.reels:
            if reel.candidate_id in seen_ids:
                continue
            seen_ids.add(reel.candidate_id)
            all_reels.append(reel)
        all_raw.extend(
            r for r in result.raw_rankings if r.get("candidate_id") not in seen_ids
        )
        usage.input_tokens += result.usage.input_tokens
        usage.output_tokens += result.usage.output_tokens
        if start + BATCH_SIZE >= len(candidates):
            break

    return RankingResult(reels=all_reels, usage=usage, raw_rankings=all_raw)


def _accumulate_usage(resp: Any) -> UsageTotals:
    u = UsageTotals()
    usage = getattr(resp, "usage", None)
    if usage is not None:
        u.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        u.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
    return u
