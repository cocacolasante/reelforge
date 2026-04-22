from __future__ import annotations

import pytest

from reelforge_core.pricing import (
    PRICING,
    PRICING_AS_OF,
    estimate_ranking_cost,
    estimate_semantics_cost,
    price_for,
)


def test_pricing_as_of_is_populated() -> None:
    # Sanity: if someone refreshes prices they bump the date. Guards against
    # forgetting to update the provenance comment.
    assert isinstance(PRICING_AS_OF, str) and len(PRICING_AS_OF) == 10


def test_price_for_known_model_uses_table() -> None:
    # Haiku: 1 / 5 per million
    assert price_for("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(6.0)
    # Sonnet: 3 / 15
    assert price_for("claude-sonnet-4-5", 1_000_000, 1_000_000) == pytest.approx(18.0)
    # Opus: 15 / 75
    assert price_for("claude-opus-4-7", 1_000_000, 1_000_000) == pytest.approx(90.0)


def test_price_for_unknown_model_falls_back_to_haiku_rate() -> None:
    # Spec: under-estimate is safer than over-estimate on unknown models.
    assert price_for("claude-unknown-x", 1_000_000, 1_000_000) == pytest.approx(6.0)


def test_price_for_zero_tokens_zero_cost() -> None:
    assert price_for("claude-haiku-4-5", 0, 0) == 0.0


def test_price_rounds_to_four_decimals() -> None:
    # A tiny call should still report a non-zero number at 4 dp.
    cost = price_for("claude-haiku-4-5", 1_000, 0)
    assert cost == pytest.approx(0.001)
    # A sub-tick-four call rounds down.
    assert price_for("claude-haiku-4-5", 1, 0) == 0.0


def test_estimate_semantics_scales_with_scene_count() -> None:
    one = estimate_semantics_cost(
        scene_count=1, model="claude-haiku-4-5"
    )["estimated_cost_usd"]
    ten = estimate_semantics_cost(
        scene_count=10, model="claude-haiku-4-5"
    )["estimated_cost_usd"]
    assert ten > one
    # ~10x (ratio check for small N — avoid flake by wide bound)
    assert ten / max(one, 1e-9) > 5


def test_estimate_semantics_zero_scenes() -> None:
    b = estimate_semantics_cost(scene_count=0, model="claude-haiku-4-5")
    assert b["expected_calls"] == 0
    assert b["estimated_cost_usd"] == 0.0


def test_estimate_semantics_cache_hits_reduce_cost() -> None:
    full = estimate_semantics_cost(scene_count=10, model="claude-haiku-4-5")
    with_cache = estimate_semantics_cost(
        scene_count=10, model="claude-haiku-4-5", cache_hits=5
    )
    assert with_cache["expected_calls"] == 5
    assert with_cache["estimated_cost_usd"] < full["estimated_cost_usd"]


def test_estimate_ranking_zero_candidates() -> None:
    b = estimate_ranking_cost(candidate_count=0, model="claude-sonnet-4-5")
    assert b["expected_calls"] == 0
    assert b["estimated_cost_usd"] == 0.0


def test_estimate_ranking_scales_with_candidates() -> None:
    one = estimate_ranking_cost(
        candidate_count=1, model="claude-sonnet-4-5"
    )["estimated_cost_usd"]
    fifty = estimate_ranking_cost(
        candidate_count=50, model="claude-sonnet-4-5"
    )["estimated_cost_usd"]
    assert fifty > one
    # Output tokens scale linearly; input has a system-prompt floor. Ratio
    # should be at least 5× and under 100×.
    ratio = fifty / max(one, 1e-9)
    assert 5 < ratio < 100


def test_pricing_table_covers_known_models() -> None:
    # If a required model is missing from PRICING the estimate paths silently
    # fall back to Haiku — assert the models this codebase actually uses are
    # present by name.
    for m in [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5",
    ]:
        assert m in PRICING
