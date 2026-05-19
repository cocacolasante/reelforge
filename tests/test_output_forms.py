"""SelectionConfig output_form + candidate enumeration honors the new modes."""

from __future__ import annotations

from reelforge_core.models import SelectionConfig
from reelforge_core.reels import generate_candidates

from tests.reels._fixtures import make_analysis


def test_effective_bounds_short_form() -> None:
    cfg = SelectionConfig(output_form="short", target_min_sec=20, target_max_sec=45)
    assert cfg.effective_min_sec == 20
    assert cfg.effective_max_sec == 45


def test_effective_bounds_long_single_widens_around_target() -> None:
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=600)
    assert cfg.effective_min_sec == 510  # 0.85 * 600
    assert cfg.effective_max_sec == 690  # 1.15 * 600


def test_effective_bounds_long_single_without_target_falls_back() -> None:
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=None)
    assert cfg.effective_min_sec == 30  # short defaults
    assert cfg.effective_max_sec == 60


def test_effective_max_scenes_raises_for_long_single() -> None:
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=300)
    # Short default is 6 scenes; long_single bumps to at least 60 so a 5-minute
    # span across many short scenes isn't truncated.
    assert cfg.effective_max_scenes >= 60


def test_long_single_enumeration_uses_widened_bounds() -> None:
    # 30 scenes of 15s each → 450s total. A long_single target of 300s should
    # yield candidates clustered around 300s.
    analysis = make_analysis("aid", [15.0] * 30)
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=300)
    cands = generate_candidates(analysis, cfg)
    assert cands, "expected candidates around the 300s target"
    durations = [c.duration_sec for c in cands]
    # Every candidate should fall in [255, 345] (±15% of target)
    for d in durations:
        assert 255 <= d <= 345
    # And we should have multiple — the enumerator slides a window across the source.
    assert len(cands) >= 3


def test_short_form_unchanged_default() -> None:
    analysis = make_analysis("aid", [5.0] * 12)  # 60s of 5s scenes
    cfg = SelectionConfig()  # defaults: short, 30-60s
    cands = generate_candidates(analysis, cfg)
    for c in cands:
        assert 30 <= c.duration_sec <= 60


def test_long_montage_enumeration_matches_short() -> None:
    # long_montage uses the same enumeration as short; the difference is what
    # happens AFTER selection (a compile step). Candidate sets must be identical
    # for the same min/max.
    analysis = make_analysis("aid", [5.0] * 12)
    short_cfg = SelectionConfig(output_form="short", target_min_sec=30, target_max_sec=60)
    mont_cfg = SelectionConfig(output_form="long_montage", target_min_sec=30, target_max_sec=60)
    short_ids = {c.candidate_id for c in generate_candidates(analysis, short_cfg)}
    mont_ids = {c.candidate_id for c in generate_candidates(analysis, mont_cfg)}
    assert short_ids == mont_ids
