"""CP1 (pro-editing plan): hard cuts by default, one flashy transition per
reel at most, one dip to black per cinematic reel, sharpening only where it
helps, smooth LUTs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.compose.director import MAX_FLASHY_PER_REEL, apply_director
from reelforge_core.compose.pipeline import _all_sources_downscaled
from reelforge_core.compose.reframe import MAX_DRIFT, clamp_drift
from reelforge_core.compose.styles import EditPlan, PlannedShot, cinematic_cuts
from reelforge_core.models import ComposeConfig
from tests.compose.test_speech_snap import _analysis, _scene


# --- transitions -------------------------------------------------------------------


def test_cinematic_dissolves_with_one_dip_before_the_end():
    assert cinematic_cuts(1) == []
    assert cinematic_cuts(2) == [("dissolve", 0.8)]  # too short to earn a dip
    assert cinematic_cuts(5) == [("dissolve", 0.8)] * 3 + [("fadeblack", 0.8)]


def _plan(n=4, per_cut=None):
    shots = [PlannedShot(0, i * 3.0, i * 3.0 + 3.0) for i in range(n)]
    return EditPlan(style="hype", shots=shots, per_cut=per_cut or [("cut", 0.04)] * (n - 1))


def test_director_gets_one_flashy_transition_and_the_rest_stay_cuts():
    raw = {"cuts": [{"index": i, "kind": "slideleft", "duration_sec": 0.25} for i in range(3)]}
    plan, _overlay, _applied = apply_director(_plan(), raw, _analysis([_scene(0, 0, 12)], None))
    kinds = [c[0] for c in plan.per_cut]
    assert kinds.count("slideleft") == MAX_FLASHY_PER_REEL == 1
    assert kinds.count("cut") == 2


def test_director_may_swap_one_flashy_transition_for_another():
    base = _plan(per_cut=[("fadewhite", 0.2), ("cut", 0.04), ("cut", 0.04)])
    raw = {"cuts": [{"index": 0, "kind": "slideright", "duration_sec": 0.25},
                    {"index": 1, "kind": "slideleft", "duration_sec": 0.25}]}
    plan, _, _ = apply_director(base, raw, _analysis([_scene(0, 0, 12)], None))
    assert [c[0] for c in plan.per_cut] == ["slideright", "cut", "cut"]


# --- sharpening -------------------------------------------------------------------


def _clip(asset_id):
    return SimpleNamespace(asset_id=asset_id, is_photo=False)


def _media(height):
    return SimpleNamespace(probe=SimpleNamespace(height=height))


def test_sharpening_is_skipped_only_when_every_source_is_downscaled():
    cfg = ComposeConfig()  # 1080x1920 output
    four_k, hd = _media(2160), _media(1080)
    assert _all_sources_downscaled([_clip("a")], {"a": four_k}, four_k, cfg) is True
    # One upscaled 1080p clip keeps the (mild) pass on.
    assert _all_sources_downscaled([_clip("a"), _clip("b")], {"a": four_k, "b": hd}, four_k, cfg) is False
    assert _all_sources_downscaled([_clip("a")], {"a": _media(None)}, four_k, cfg) is False


# --- reframe -------------------------------------------------------------------------


@pytest.mark.parametrize("x0,x1", [(0.2, 0.8), (0.8, 0.2), (0.1, 0.9)])
def test_drift_is_capped_and_keeps_its_direction(x0, x1):
    a, b = clamp_drift(x0, x1)
    assert abs(b - a) == pytest.approx(MAX_DRIFT)
    assert (b > a) == (x1 > x0)
    assert (a + b) / 2 == pytest.approx((x0 + x1) / 2)


def test_small_drift_passes_through():
    assert clamp_drift(0.45, 0.55) == (0.45, 0.55)


# --- LUTs ------------------------------------------------------------------------------


def _load_cube(path: Path):
    rows = [tuple(map(float, ln.split())) for ln in path.read_text().splitlines() if ln[:1].isdigit()]
    return rows, round(len(rows) ** (1 / 3))


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_luts_have_no_steps_and_keep_black_and_white(tmp_path: Path):
    """The old cinematic LUT switched grades at luma 0.4: a step 2.77x the
    grid spacing, drawn as a band across any gradient that crossed it."""
    script = Path(__file__).resolve().parents[2] / "assets" / "luts" / "synthesize_luts.sh"
    subprocess.run(["bash", str(script), str(tmp_path)], check=True, capture_output=True)
    for name in ("warm", "cool", "cinematic", "vivid"):
        rows, n = _load_cube(tmp_path / f"{name}.cube")
        at = lambda r, g, b: rows[b * n * n + g * n + r]  # noqa: E731
        worst = max(
            max(abs(x - y) for x, y in zip(at(r, g, b), at(r + 1, g, b)))
            for b in range(n) for g in range(n) for r in range(n - 1)
        )
        assert worst <= 1.35 / (n - 1), f"{name} has a step of {worst:.3f}"
    for name in ("warm", "cool", "cinematic"):
        rows, _ = _load_cube(tmp_path / f"{name}.cube")
        assert rows[0] == (0.0, 0.0, 0.0) and rows[-1] == (1.0, 1.0, 1.0), name
