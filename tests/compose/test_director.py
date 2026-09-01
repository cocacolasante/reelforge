"""Edit Quality CP5: AI edit-director — validation, stamping, failure paths."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.compose.director import (
    apply_director,
    build_director_context,
    plan_fingerprint,
    run_director,
)
from reelforge_core.compose.styles import EditPlan, PlannedShot
from reelforge_core.models import ComposeConfig, TranscriptWord
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _plan(style: str = "hype") -> EditPlan:
    return EditPlan(
        style=style,
        shots=[
            PlannedShot(0, 0.0, 3.0),
            PlannedShot(0, 3.0, 6.0),
            PlannedShot(0, 6.0, 10.0),
        ],
        per_cut=[("cut", 0.04), ("cut", 0.04)],
    )


def _an():
    return _analysis([_scene(0, 0, 60)], None)


# ---- apply_director validation ---------------------------------------------


def test_apply_empty_proposal_is_identity():
    plan = _plan()
    new_plan, overlay, applied = apply_director(
        plan, {"shots": [], "cuts": [], "hook_text": None}, _an()
    )
    assert [(s.in_ts, s.out_ts) for s in new_plan.shots] == [(0.0, 3.0), (3.0, 6.0), (6.0, 10.0)]
    assert new_plan.per_cut == plan.per_cut
    assert overlay is None and applied == []


def test_apply_nudges_within_bounds():
    raw = {
        "shots": [{"index": 1, "nudge_start_sec": -0.5, "nudge_end_sec": 1.0, "reason": "open on the jump"}],
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, applied = apply_director(_plan(), raw, _an())
    assert (new_plan.shots[1].in_ts, new_plan.shots[1].out_ts) == (2.5, 7.0)
    assert applied and "shot 1" in applied[0]


def test_apply_clamps_oversized_nudges():
    raw = {
        "shots": [{"index": 0, "nudge_start_sec": -9.0, "nudge_end_sec": 9.0, "reason": "x"}],
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, _ = apply_director(_plan(), raw, _an())
    # start clamps to in-1.5 then to >=0; end clamps to out+1.5.
    assert new_plan.shots[0].in_ts == 0.0
    assert new_plan.shots[0].out_ts == pytest.approx(4.5)


def test_apply_rejects_off_palette_cut_and_clamps_duration():
    raw = {
        "shots": [],
        "cuts": [
            {"index": 0, "kind": "circleopen", "reason": "not in hype palette"},
            {"index": 1, "kind": "slideleft", "duration_sec": 1.4, "reason": "ok kind, too long"},
        ],
        "hook_text": None,
    }
    new_plan, _, applied = apply_director(_plan("hype"), raw, _an())
    assert new_plan.per_cut[0] == ("cut", 0.04)  # rejected -> untouched
    assert new_plan.per_cut[1] == ("slideleft", 0.35)  # clamped to hype max
    assert len(applied) == 1


def test_apply_rejects_disallowed_speed_and_punch():
    raw = {
        "shots": [
            {"index": 0, "speed": 3.0, "reason": "not in hype set"},
            {"index": 1, "speed": 1.5, "punch_in": 2.5, "reason": "speed ok, punch too big"},
        ],
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, _ = apply_director(_plan("hype"), raw, _an())
    assert new_plan.shots[0].speed == 1.0  # reverted
    assert new_plan.shots[1].speed == 1.5
    assert new_plan.shots[1].punch_in is None  # reverted


def test_apply_reverts_geometry_that_breaks_min_shot():
    # Nudging shot 0 to 0.4s source at speed 1 violates hype min_shot 0.6.
    raw = {
        "shots": [{"index": 0, "nudge_end_sec": -1.5, "nudge_start_sec": 1.1, "reason": "x"}],
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, _ = apply_director(_plan("hype"), raw, _an())
    assert (new_plan.shots[0].in_ts, new_plan.shots[0].out_ts) == (0.0, 3.0)


def test_apply_snaps_mid_word_nudges():
    words = [TranscriptWord(start=2.3, end=2.9, word="hey", probability=0.9)]
    analysis = _analysis([_scene(0, 0, 60)], words)
    raw = {
        "shots": [{"index": 1, "nudge_start_sec": -0.5, "reason": "x"}],  # 3.0 -> 2.5, mid-word
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, _ = apply_director(_plan(), raw, analysis)
    assert new_plan.shots[1].in_ts == pytest.approx(2.3)  # snapped to word start


def test_apply_hook_overlay_truncated():
    raw = {"shots": [], "cuts": [], "hook_text": "  " + "X" * 60}
    _, overlay, applied = apply_director(_plan(), raw, _an())
    assert overlay is not None
    assert len(overlay.text) == 40
    assert overlay.position == "top" and overlay.start_sec == 0.4
    assert any("hook overlay" in a for a in applied)


def test_apply_skips_bogus_indices():
    raw = {
        "shots": [{"index": 99, "nudge_start_sec": 1.0, "reason": "x"}],
        "cuts": [{"index": 99, "kind": "cut", "reason": "x"}],
        "hook_text": None,
    }
    new_plan, _, applied = apply_director(_plan(), raw, _an())
    assert applied == []
    assert new_plan.per_cut == _plan().per_cut


# ---- fingerprint -----------------------------------------------------------


def test_fingerprint_tracks_plan_and_style_and_model():
    p = _plan()
    base = plan_fingerprint(p, "hype", "m1")
    assert plan_fingerprint(p, "hype", "m1") == base
    assert plan_fingerprint(p, "chill", "m1") != base
    assert plan_fingerprint(p, "hype", "m2") != base
    moved = EditPlan(style=p.style, shots=[PlannedShot(0, 0.5, 3.0)] + p.shots[1:], per_cut=p.per_cut)
    assert plan_fingerprint(moved, "hype", "m1") != base


# ---- run_director: call, stamp, failure ------------------------------------


class _FakeDirector:
    def __init__(self, payload=None, raise_exc: Exception | None = None):
        self.payload = payload or {"shots": [], "cuts": [], "hook_text": None}
        self.raise_exc = raise_exc
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        block = SimpleNamespace(type="tool_use", input=self.payload, name="record_edit_plan")
        return SimpleNamespace(
            content=[block],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=500, output_tokens=80),
        )


async def test_run_director_applies_and_stamps(tmp_path: Path):
    plan = _plan()
    payload = {
        "shots": [{"index": 0, "nudge_end_sec": 0.5, "reason": "carry the landing"}],
        "cuts": [{"index": 0, "kind": "slideleft", "duration_sec": 0.25, "reason": "match motion"}],
        "hook_text": "WAIT FOR IT",
    }
    client = _FakeDirector(payload)
    new_plan, overlay, usage = await run_director(
        plan, _reel([0], 0.0, 10.0), _an(), ComposeConfig(), None, tmp_path, client=client
    )
    assert client.calls == 1
    assert new_plan.shots[0].out_ts == pytest.approx(3.5)
    assert new_plan.per_cut[0] == ("slideleft", 0.25)
    assert overlay is not None and overlay.text == "WAIT FOR IT"
    assert usage.input_tokens == 500
    assert (tmp_path / "director_raw.json").exists()
    assert (tmp_path / "director_raw.json.stamp").exists()

    # Second run with an identical plan: stamp hit, no call, same result.
    client2 = _FakeDirector(payload)
    replay_plan, replay_overlay, replay_usage = await run_director(
        plan, _reel([0], 0.0, 10.0), _an(), ComposeConfig(), None, tmp_path, client=client2
    )
    assert client2.calls == 0
    assert replay_plan.shots[0].out_ts == pytest.approx(3.5)
    assert replay_overlay is not None
    assert replay_usage.input_tokens == 0


async def test_run_director_failure_keeps_plan(tmp_path: Path):
    plan = _plan()
    client = _FakeDirector(raise_exc=ValueError("boom"))
    new_plan, overlay, usage = await run_director(
        plan, _reel([0], 0.0, 10.0), _an(), ComposeConfig(), None, tmp_path, client=client
    )
    assert new_plan is plan
    assert overlay is None and usage.input_tokens == 0
    assert not (tmp_path / "director_raw.json").exists()  # failures never stamp


def test_context_carries_constraints_and_shots():
    ctx = build_director_context(_plan("hype"), _reel([0], 0.0, 10.0), _an(), None)
    assert ctx["style"] == "hype"
    assert "cut" in ctx["constraints"]["transition_palette"]
    assert len(ctx["shots"]) == 3 and len(ctx["cuts"]) == 2
    assert ctx["constraints"]["max_nudge_sec"] == 1.5
