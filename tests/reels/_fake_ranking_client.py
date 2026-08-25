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

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls: list[dict] = []
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
        text = self._user_text(kwargs)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [c["candidate_id"] for c in parsed.get("candidates", [])]

    async def create(self, **kwargs: Any) -> Any:
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
