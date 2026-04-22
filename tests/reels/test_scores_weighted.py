from __future__ import annotations

from reelforge_core.models import ReelScores


def test_weights_sum_to_one() -> None:
    total = 0.35 + 0.30 + 0.20 + 0.15
    assert abs(total - 1.0) < 1e-9


def test_all_max_gives_100() -> None:
    s = ReelScores(
        narrative_coherence=100,
        hook_strength=100,
        emotional_payoff=100,
        standalone_clarity=100,
    )
    assert abs(s.weighted - 100.0) < 1e-9


def test_all_zero_gives_zero() -> None:
    s = ReelScores(
        narrative_coherence=0,
        hook_strength=0,
        emotional_payoff=0,
        standalone_clarity=0,
    )
    assert s.weighted == 0.0


def test_weighted_formula_exact() -> None:
    s = ReelScores(
        narrative_coherence=60,
        hook_strength=80,
        emotional_payoff=40,
        standalone_clarity=20,
    )
    expected = 0.35 * 80 + 0.30 * 60 + 0.20 * 40 + 0.15 * 20
    assert abs(s.weighted - expected) < 1e-9
