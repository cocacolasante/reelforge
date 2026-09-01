"""Local cost estimates for UX only.

This is a best-effort local estimate. Bill from your Anthropic console, not
from this table — rates change and cache/batch discounts aren't modeled here.

Update `PRICING_AS_OF` whenever you refresh rates.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_AS_OF = "2026-04-22"


@dataclass(frozen=True)
class ModelPricing:
    model: str
    input_per_million: float   # USD per 1M input tokens
    output_per_million: float  # USD per 1M output tokens


# Published list prices for the Claude 4.5/4.6 family as of PRICING_AS_OF.
# Numbers here are intentionally conservative — actual invoice will be equal or
# slightly lower thanks to caching and batching.
PRICING: dict[str, ModelPricing] = {
    "claude-haiku-4-5-20251001": ModelPricing(
        model="claude-haiku-4-5-20251001",
        input_per_million=1.00,
        output_per_million=5.00,
    ),
    "claude-haiku-4-5": ModelPricing(
        model="claude-haiku-4-5",
        input_per_million=1.00,
        output_per_million=5.00,
    ),
    "claude-sonnet-4-5": ModelPricing(
        model="claude-sonnet-4-5",
        input_per_million=3.00,
        output_per_million=15.00,
    ),
    "claude-sonnet-4-6": ModelPricing(
        model="claude-sonnet-4-6",
        input_per_million=3.00,
        output_per_million=15.00,
    ),
    "claude-opus-4-7": ModelPricing(
        model="claude-opus-4-7",
        input_per_million=15.00,
        output_per_million=75.00,
    ),
}


def price_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the estimated USD cost for a single call."""
    p = PRICING.get(model)
    if p is None:
        # Unknown model: assume the Haiku rate. Under-estimation is safer than
        # over-estimation for UX.
        p = PRICING["claude-haiku-4-5"]
    input_cost = (input_tokens / 1_000_000.0) * p.input_per_million
    output_cost = (output_tokens / 1_000_000.0) * p.output_per_million
    return round(input_cost + output_cost, 4)


# Heuristic constants used to estimate token counts before the call fires.
# Derived from measured Phase-1 and Phase-2 runs on typical content; off-by-2×
# at worst. The UI labels these as estimates.
AVG_SEMANTICS_PROMPT_TOKENS = 900        # system + scene frame (image) context
AVG_SEMANTICS_OUTPUT_TOKENS = 130
# Calibrated against a live v2 run (silent 119s asset, 6 candidates:
# 8,175 in / 1,278 out); speech-heavy contexts run larger.
AVG_RANKING_PROMPT_PER_CANDIDATE = 800   # v2 rich context (words/energy/features)
AVG_RANKING_SYSTEM_TOKENS = 1200         # v2 listwise prompt + intro block
AVG_RANKING_OUTPUT_PER_CANDIDATE = 200   # v2 adds rank_position + opening_description
# Contact sheet ≈ width*height/750 image tokens; 3 tiles at 180px height from
# 16:9 source ≈ 960x180 ≈ 230 — rounded up for taller-than-16:9 tiles.
AVG_CONTACT_SHEET_TOKENS = 250
# Edit director (compose): one call per (changed) plan. Calibrated against a
# live 22-shot hype run: 4,525 in / 408 out.
AVG_DIRECTOR_SYSTEM_TOKENS = 900
AVG_DIRECTOR_PER_SHOT_TOKENS = 165
AVG_DIRECTOR_OUTPUT_TOKENS = 400


def estimate_semantics_cost(
    *,
    scene_count: int,
    model: str,
    cache_hits: int = 0,
) -> dict:
    """Estimate Phase-1 semantics cost. Returns a breakdown dict."""
    scene_count = max(0, scene_count)
    effective_calls = max(0, scene_count - cache_hits)
    input_tokens = effective_calls * AVG_SEMANTICS_PROMPT_TOKENS
    output_tokens = effective_calls * AVG_SEMANTICS_OUTPUT_TOKENS
    cost = price_for(model, input_tokens, output_tokens)
    return {
        "stage": "semantics",
        "model": model,
        "expected_calls": effective_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": cost,
    }


def estimate_ranking_cost(
    *,
    candidate_count: int,
    model: str,
) -> dict:
    """Estimate Phase-2 ranking cost. One batched call; token count scales with
    candidate context length."""
    candidate_count = max(0, candidate_count)
    if candidate_count == 0:
        return {
            "stage": "ranking",
            "model": model,
            "expected_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    input_tokens = (
        AVG_RANKING_SYSTEM_TOKENS
        + candidate_count
        * (AVG_RANKING_PROMPT_PER_CANDIDATE + AVG_CONTACT_SHEET_TOKENS)
    )
    output_tokens = candidate_count * AVG_RANKING_OUTPUT_PER_CANDIDATE
    cost = price_for(model, input_tokens, output_tokens)
    return {
        "stage": "ranking",
        "model": model,
        "expected_calls": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": cost,
    }
