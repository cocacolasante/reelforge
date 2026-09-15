"""Requested reel lengths that the source can't provide are shrunk to fit,
instead of silently enumerating zero candidates."""

from __future__ import annotations

from reelforge_core.models import SelectionConfig
from reelforge_core.reels.pipeline import fit_to_source


def test_long_single_longer_than_source_uses_the_whole_clip():
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=300.0)
    fitted = fit_to_source(cfg, 113.766667)
    assert fitted.long_target_duration_sec == 113.767
    assert fitted.effective_min_sec <= 113.766667 <= fitted.effective_max_sec


def test_long_single_that_fits_is_untouched():
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=120.0)
    assert fit_to_source(cfg, 150.0) is cfg


def test_short_minimum_longer_than_source_shrinks():
    cfg = SelectionConfig(target_min_sec=30.0, target_max_sec=60.0)
    fitted = fit_to_source(cfg, 20.0)
    assert (fitted.target_min_sec, fitted.target_max_sec) == (10.0, 20.0)
    assert fit_to_source(SelectionConfig(), 90.0).target_min_sec == 30.0


def test_unknown_duration_is_a_noop():
    cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=300.0)
    assert fit_to_source(cfg, 0.0) is cfg
