"""Mock AsyncAnthropic that records calls and returns canned tool-use responses.

Supports a scripted sequence of responses so tests can simulate missing-candidate
retries and malformed responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass
class _ToolUseBlock:
    type: str
    input: dict
    id: str = "toolu_01"
    name: str = "record_rankings"


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 200


def _ranking_for(candidate_id: str, seed: int = 0, relevance: int | None = None) -> dict:
    base_scores = {
        "narrative_coherence": (40 + seed * 7) % 101,
        "hook_strength": (50 + seed * 11) % 101,
        "emotional_payoff": (30 + seed * 13) % 101,
        "standalone_clarity": (60 + seed * 3) % 101,
    }
    moods = [
        "calm",
        "tense",
        "joyful",
        "energetic",
        "somber",
        "romantic",
        "mysterious",
        "triumphant",
        "melancholic",
        "neutral",
    ]
    entry = {
        "candidate_id": candidate_id,
        "title": f"Title for {candidate_id}",
        "hook": f"Hook teaser for {candidate_id}.",
        "justification": f"Ranked because of reasons specific to {candidate_id}.",
        "suggested_mood": moods[seed % len(moods)],
        "scores": base_scores,
        # v2 required fields — harmless extras under a v1-style schema.
        "rank_position": seed + 1,
        "opening_description": f"Opening frame of {candidate_id}"[:80],
    }
    if relevance is not None:
        entry["prompt_relevance"] = relevance
    return entry


def all_rankings(
    candidate_ids: list[str],
    relevance: int | dict[str, int] | None = None,
) -> list[dict]:
    """relevance: None (no field), an int (same for all), or {cid: int}."""
    out = []
    for i, cid in enumerate(candidate_ids):
        rel = relevance.get(cid, 50) if isinstance(relevance, dict) else relevance
        out.append(_ranking_for(cid, seed=i, relevance=rel))
    return out


class FakeRankingClient:
    """Scripted fake. Each `messages.create` call returns the next scripted payload.

    If the script is exhausted, the last payload is reused (so tests that don't
    care about sequencing still work).
    """

    def __init__(self, script: list[dict], refine_script: list[dict] | None = None):
        self.script = list(script)
        self.calls: list[dict] = []
        # record_refinements calls are tracked separately so ranking-call
        # count assertions stay stable. Default response: empty refinements
        # (a no-op refinement pass).
        self.refine_script = list(refine_script) if refine_script else []
        self.refine_calls: list[dict] = []
        self.messages = self

    def _user_text(self, kwargs: dict) -> str:
        msgs = kwargs.get("messages", [])
        if not msgs:
            return ""
        content = msgs[0].get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    return block["text"]
        return ""

    def _candidate_ids_from_payload(self, kwargs: dict) -> list[str]:
        """Candidate ids from either message layout: the v1 single JSON string
        ({"candidates": [...]}) or the v2 interleaved blocks (one JSON text
        block per candidate, with image blocks between)."""
        msgs = kwargs.get("messages", [])
        content = msgs[0].get("content") if msgs else None
        texts: list[str] = []
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [b["text"] for b in content if b.get("type") == "text"]
        ids: list[str] = []
        for text in texts:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "candidate_id" in parsed:
                ids.append(parsed["candidate_id"])
            elif isinstance(parsed, dict):
                ids.extend(c["candidate_id"] for c in parsed.get("candidates", []))
        return ids

    async def create(self, **kwargs: Any) -> Any:
        tools = kwargs.get("tools") or []
        if tools and tools[0].get("name") == "record_refinements":
            self.refine_calls.append(kwargs)
            if self.refine_script:
                payload = (
                    self.refine_script.pop(0)
                    if len(self.refine_script) > 1
                    else self.refine_script[0]
                )
            else:
                payload = {"refinements": []}
            return SimpleNamespace(
                content=[
                    _ToolUseBlock(
                        type="tool_use", input=payload, name="record_refinements"
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=40, output_tokens=30),
            )
        self.calls.append(kwargs)
        if not self.script:
            rankings = all_rankings(self._candidate_ids_from_payload(kwargs))
        else:
            payload = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if payload.get("_use_input_ids"):
                rankings = all_rankings(self._candidate_ids_from_payload(kwargs))
                missing = payload.get("_drop_ids", [])
                rankings = [r for r in rankings if r["candidate_id"] not in missing]
                extra_ids = payload.get("_extra_ids", [])
                for idx, extra in enumerate(extra_ids):
                    rankings.append(_ranking_for(extra, seed=100 + idx))
            else:
                rankings = payload["rankings"]

        return SimpleNamespace(
            content=[_ToolUseBlock(type="tool_use", input={"rankings": rankings})],
            stop_reason="tool_use",
            usage=_Usage(),
        )
