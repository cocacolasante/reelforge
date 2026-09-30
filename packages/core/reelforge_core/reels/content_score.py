"""Content scores for the shortlist (pro-editing CP7).

The prescore shortlist ranks candidates by edges, energy and scene cuts —
none of which knows what anyone SAID, so a strong line delivered over calm
footage never reached the ranker. This scores the words themselves: ONE
stamped, text-only call (`record_line_scores`, Haiku by default) rates every
utterance unit of the asset twice, 0-10 —

- `hook`: how well the line would OPEN a video (makes a scroller stop);
- `payoff`: how well it would END one (lands a point, a laugh, a result).

A candidate's content score is hook(its opening unit) + payoff(its closing
unit), 0-20. `prescore.shortlist` reserves RESERVED_SLOTS of the shortlist
for the best content-scored candidates the heuristic walk missed. Failures
(or no speech) return {} and the shortlist is the heuristic one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from reelforge_core.models import ReelCandidate, UsageTotals

log = logging.getLogger(__name__)

CONTENT_SCORE_VERSION = "c1"
CONTENT_MODEL = "claude-haiku-4-5-20251001"
MAX_UNITS = 500  # ~40+ min of talk; units past this keep heuristic-only
MAX_UNIT_CHARS = 240

SYSTEM_PROMPT = """You rate lines of speech from raw footage for a video \
editor choosing short-form clips.

Each numbered line is one utterance. Rate EVERY line twice, 0-10:
- hook: as the FIRST line of a short video, would it make a scrolling viewer \
stop? High: a bold claim, a surprising fact, a question, a number, a \
conflict, a strong emotion. Low: greetings, filler, setup that needs context \
("so as I was saying"), logistics.
- payoff: as the LAST line, does it land? High: a result, a punchline, a \
conclusion, a reveal, a strong reaction. Low: trailing off, mid-thought, \
filler, "anyway".
Most lines are ordinary: use the whole range and keep 8-10 rare."""

RECORD_LINE_SCORES: dict[str, Any] = {
    "name": "record_line_scores",
    "description": "Record hook and payoff scores for every line.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer"},
                        "hook": {"type": "integer", "minimum": 0, "maximum": 10},
                        "payoff": {"type": "integer", "minimum": 0, "maximum": 10},
                    },
                    "required": ["line", "hook", "payoff"],
                },
            }
        },
        "required": ["scores"],
    },
}


def _fingerprint(units: list, model: str) -> str:
    blob = json.dumps(
        [CONTENT_SCORE_VERSION, model, [[round(u.start, 2), u.text[:MAX_UNIT_CHARS]] for u in units]],
        separators=(",", ":"),
    )
    return hashlib.sha1(blob.encode()).hexdigest()


def parse_scores(raw: dict, n_units: int) -> dict[int, tuple[int, int]]:
    """{unit index: (hook, payoff)} for every entry that validates. Pure."""
    out: dict[int, tuple[int, int]] = {}
    for entry in raw.get("scores", []) or []:
        try:
            i, hook, payoff = int(entry["line"]), int(entry["hook"]), int(entry["payoff"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n_units and 0 <= hook <= 10 and 0 <= payoff <= 10 and i not in out:
            out[i] = (hook, payoff)
    return out


def candidate_scores(
    candidates: list[ReelCandidate],
    units: list,
    unit_scores: dict[int, tuple[int, int]],
) -> dict[str, float]:
    """Candidate id -> hook(opening unit) + payoff(closing unit). A
    candidate whose opening or closing unit went unscored gets no score. Pure."""
    out: dict[str, float] = {}
    for c in candidates:
        inside = [
            i for i, u in enumerate(units)
            if u.end > c.start_sec + 1e-3 and u.start < c.end_sec - 1e-3
        ]
        if not inside:
            continue
        first, last = unit_scores.get(inside[0]), unit_scores.get(inside[-1])
        if first is None or last is None:
            continue
        out[c.candidate_id] = float(first[0] + last[1])
    return out


def _extract(resp: Any) -> dict:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            inp = getattr(block, "input", None)
            if isinstance(inp, str):
                inp = json.loads(inp)
            if isinstance(inp, dict) and "scores" in inp:
                return inp
    raise ValueError("no record_line_scores tool_use block in response")


def _default_client() -> Any:
    """Replaced in tests (tests/conftest.py) — the test service loads .env."""
    from anthropic import AsyncAnthropic

    return AsyncAnthropic()


async def score_units(
    units: list,
    *,
    working_dir: Path,
    model: str = CONTENT_MODEL,
    client: Any | None = None,
) -> tuple[dict[int, tuple[int, int]], UsageTotals]:
    """Stamped (`content_scores.json`): the same units + model never pay
    twice. Returns ({}, usage) on any failure."""
    from reelforge_core.io_utils import write_json_atomic
    from reelforge_core.reels.rank import _accumulate_usage, _call_model

    units = units[:MAX_UNITS]
    if not units:
        return {}, UsageTotals()
    path = working_dir / "content_scores.json"
    fp = _fingerprint(units, model)
    try:
        cached = json.loads(path.read_text())
        if cached.get("fingerprint") == fp:
            return parse_scores(cached.get("raw", {}), len(units)), UsageTotals()
    except (OSError, ValueError):
        pass
    lines = "\n".join(f"{i}: {u.text[:MAX_UNIT_CHARS]}" for i, u in enumerate(units))
    try:
        if client is None:
            client = _default_client()
        resp = await _call_model(
            client,
            model=model,
            temperature=0.0,
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": lines}],
            tools=[RECORD_LINE_SCORES],
            tool_name="record_line_scores",
            max_tokens=min(32000, 400 + 24 * len(units)),
        )
        raw = _extract(resp)
        usage = _accumulate_usage(resp)
    except Exception as exc:  # noqa: BLE001 — the heuristic shortlist still works
        log.warning("content scoring failed; heuristic shortlist: %s", exc)
        return {}, UsageTotals()
    scores = parse_scores(raw, len(units))
    write_json_atomic(path, {"version": CONTENT_SCORE_VERSION, "model": model,
                             "fingerprint": fp, "raw": raw, "usage": usage.model_dump()})
    log.info("content scores: %d/%d lines (in %d / out %d tokens)",
             len(scores), len(units), usage.input_tokens, usage.output_tokens)
    return scores, usage
