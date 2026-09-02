"""AI Mix CP1: sequencing call, validation-with-fallback."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reelforge_core.mixes.mining import MinedMoment
from reelforge_core.mixes.sequencer import (
    build_moment_context,
    fallback_sequence,
    sequence_mix,
    validate_sequence,
)
from reelforge_core.models import ReelCandidate
from reelforge_core.reels.prescore import PrescoreFeatures

from tests.reels._fixtures import make_analysis


def _feats(**kw) -> PrescoreFeatures:
    base = dict(
        starts_on_unit_boundary=False,
        ends_on_unit_boundary=False,
        starts_mid_word=False,
        ends_mid_word=False,
        speech_ratio=0.0,
        energy_peak_pos=None,
        energy_peak_z=None,
        lufs_range=0.0,
        n_scene_cuts=0,
        source="scene",
    )
    base.update(kw)
    return PrescoreFeatures(**base)


def _moment(aid: str, cid: str, start: float, dur: float = 5.0, score: float = 10.0) -> MinedMoment:
    return MinedMoment(
        asset_id=aid,
        candidate=ReelCandidate(
            candidate_id=cid,
            scene_indices=[0],
            start_sec=start,
            end_sec=start + dur,
            duration_sec=dur,
            scene_count=1,
        ),
        features=_feats(),
        score=score,
    )


def _pool_and_analyses(n_per: int = 6):
    a1, a2 = "a" * 64, "b" * 64
    analyses = {
        a1: make_analysis(a1, [30.0, 30.0]),
        a2: make_analysis(a2, [30.0, 30.0]),
    }
    pool = []
    for i in range(n_per):
        pool.append(_moment(a1, f"m-a{i}", i * 8.0, score=50 - i))
        pool.append(_moment(a2, f"m-b{i}", i * 8.0, score=48 - i))
    return pool, analyses


# ---- context ---------------------------------------------------------------


def test_moment_context_shape():
    pool, analyses = _pool_and_analyses(1)
    ctx = build_moment_context(pool[0], analyses[pool[0].asset_id], "run1.mp4")
    assert ctx["moment_id"] == pool[0].moment_id
    assert ctx["source_video"] == "run1.mp4"
    assert {"features", "scene_summary", "tags", "transcript_words"} <= set(ctx)


# ---- validation ------------------------------------------------------------


def test_validate_keeps_order_and_clamps_trims():
    pool, analyses = _pool_and_analyses()
    raw = {
        "sequence": [
            {"moment_id": "m-b0", "trim_start_sec": 0.5, "reason": "hook"},
            {"moment_id": "m-a1", "trim_end_sec": 5.0},  # clamped to 1.0
            {"moment_id": "m-a0"},
        ],
        "title": "Best of both runs",
        "hook": "h",
        "suggested_mood": "energetic",
        "content_style": "hype",
    }
    mix = validate_sequence(raw, pool, target_sec=14.0, analyses=analyses)
    assert [s[0] for s in mix.shots] == ["b" * 64, "a" * 64, "a" * 64]
    assert mix.shots[0][1] == pytest.approx(0.5)  # trim_start applied
    assert mix.shots[1][2] == pytest.approx(12.0)  # 8+5 - 1.0 clamp
    assert mix.suggested_mood == "energetic" and mix.content_style == "hype"
    assert not mix.fallback


def test_validate_drops_unknown_and_duplicate_ids():
    pool, analyses = _pool_and_analyses()
    raw = {
        "sequence": [
            {"moment_id": "m-a0"},
            {"moment_id": "ghost"},
            {"moment_id": "m-a0"},
            {"moment_id": "m-b0"},
            {"moment_id": "m-b1"},
        ],
        "title": "t",
        "hook": "h",
        "suggested_mood": "calm",
        "content_style": "chill",
    }
    mix = validate_sequence(raw, pool, target_sec=15.0, analyses=analyses)
    ids = [f"{'a' if aid[0] == 'a' else 'b'}" for aid, _, _ in mix.shots]
    assert len(mix.shots) == 3
    assert ids == ["a", "b", "b"]


def test_validate_drops_same_asset_overlapping_spans():
    """Live-verified failure: the model picked three distinct moment_ids
    covering the same 14s of one clip — the render replayed the footage."""
    pool, analyses = _pool_and_analyses()
    # Three overlapping 8s windows on asset a (from the live run: 72.1-80.08,
    # 75.3-83.3, 77.89-85.79 scaled down), plus distinct b moments.
    pool = pool + [
        _moment("a" * 64, "ov-1", 10.0, dur=8.0, score=60),
        _moment("a" * 64, "ov-2", 13.0, dur=8.0, score=59),
        _moment("a" * 64, "ov-3", 16.0, dur=8.0, score=58),
    ]
    raw = {
        "sequence": [
            {"moment_id": "ov-1"},
            {"moment_id": "ov-2"},
            {"moment_id": "ov-3"},
            {"moment_id": "m-b0"},
            {"moment_id": "m-b1"},
            {"moment_id": "m-b2"},
        ],
        "title": "t",
        "hook": "h",
        "suggested_mood": "energetic",
        "content_style": "classic",
    }
    mix = validate_sequence(raw, pool, target_sec=30.0, analyses=analyses)
    a_spans = [(i, o) for aid, i, o in mix.shots if aid == "a" * 64]
    # ov-1 kept; ov-2 (5/8 = 0.63 overlap) and ov-3 vs ov-1 (2/8 = 0.25 — but
    # 0.63 vs ov-2 had it been kept) — only ov-1 and ov-3 may coexist.
    for idx, (i1, o1) in enumerate(a_spans):
        for i2, o2 in a_spans[idx + 1 :]:
            inter = min(o1, o2) - max(i1, i2)
            shorter = min(o1 - i1, o2 - i2)
            assert inter / shorter <= 0.5
    # The under-length top-up must respect the same rule.
    total = sum(o - i for _, i, o in mix.shots)
    assert total >= 30.0 * 0.8 - 1e-6


def test_fallback_sequence_skips_same_asset_overlaps():
    pool = [
        _moment("a" * 64, "ov-1", 10.0, dur=8.0, score=60),
        _moment("a" * 64, "ov-2", 13.0, dur=8.0, score=59),
        _moment("b" * 64, "b-1", 0.0, dur=8.0, score=58),
    ]
    mix = fallback_sequence(pool, target_sec=30.0)
    a_spans = [(i, o) for aid, i, o in mix.shots if aid == "a" * 64]
    assert a_spans == [(10.0, 18.0)]


def test_validate_overlength_drops_weakest_keeps_order():
    pool, analyses = _pool_and_analyses()
    # Six 5s moments = 30s against a 15s target -> drop weakest until <= 18s.
    seq = [{"moment_id": f"m-a{i}"} for i in range(6)]
    raw = {"sequence": seq, "title": "t", "hook": "h", "suggested_mood": "calm", "content_style": "classic"}
    mix = validate_sequence(raw, pool, target_sec=15.0, analyses=analyses)
    total = sum(o - i for _, i, o in mix.shots)
    assert total <= 15.0 * 1.2 + 1e-6
    assert len(mix.shots) >= 3
    # Weakest (highest index = lowest score) dropped first; order of the
    # survivors is the model's order.
    starts = [i for _, i, _ in mix.shots]
    assert starts == sorted(starts)


def test_validate_underlength_tops_up_before_final_shot():
    pool, analyses = _pool_and_analyses()
    raw = {
        "sequence": [
            {"moment_id": "m-a0"},
            {"moment_id": "m-a1"},
            {"moment_id": "m-b5"},  # the chosen payoff
        ],
        "title": "t",
        "hook": "h",
        "suggested_mood": "calm",
        "content_style": "classic",
    }
    mix = validate_sequence(raw, pool, target_sec=40.0, analyses=analyses)
    total = sum(o - i for _, i, o in mix.shots)
    assert total >= 40.0 * 0.8 - 1e-6
    # The payoff the model chose stays LAST.
    assert mix.shots[-1] == ("b" * 64, 40.0, 45.0)


def test_validate_too_few_entries_raises():
    pool, analyses = _pool_and_analyses(1)  # 2 moments total
    raw = {"sequence": [{"moment_id": "ghost"}], "title": "t", "hook": "h",
           "suggested_mood": "calm", "content_style": "classic"}
    with pytest.raises(ValueError):
        validate_sequence(raw, pool[:1], target_sec=10.0, analyses=analyses)


def test_validate_bad_mood_and_style_fall_back():
    pool, analyses = _pool_and_analyses()
    raw = {
        "sequence": [{"moment_id": "m-a0"}, {"moment_id": "m-a1"}, {"moment_id": "m-b0"}],
        "title": "t",
        "hook": "h",
        "suggested_mood": "vibey",
        "content_style": "vaporwave",
    }
    mix = validate_sequence(raw, pool, target_sec=15.0, analyses=analyses)
    assert mix.suggested_mood == "neutral"
    assert mix.content_style == "classic"


def test_fallback_sequence_round_robins_to_target():
    pool, _ = _pool_and_analyses()
    mix = fallback_sequence(pool, target_sec=22.0)
    assert mix.fallback
    total = sum(o - i for _, i, o in mix.shots)
    assert total >= 22.0
    # Pool order is balanced across assets already.
    assert {aid for aid, _, _ in mix.shots} == {"a" * 64, "b" * 64}


# ---- the call --------------------------------------------------------------


class _FakeSequencer:
    def __init__(self, payload=None, raise_exc=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        block = SimpleNamespace(type="tool_use", input=self.payload, name="record_mix")
        return SimpleNamespace(
            content=[block],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


async def test_sequence_mix_happy_path(tmp_path):
    pool, analyses = _pool_and_analyses()
    payload = {
        "sequence": [{"moment_id": "m-b0"}, {"moment_id": "m-a0"}, {"moment_id": "m-a1"}],
        "title": "Two runs, one reel",
        "hook": "h",
        "suggested_mood": "energetic",
        "content_style": "hype",
    }
    sheets = {}
    for m in pool[:2]:
        p = tmp_path / f"{m.moment_id}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0jpg")
        sheets[m.moment_id] = p
    client = _FakeSequencer(payload)
    mix, usage = await sequence_mix(
        pool,
        analyses,
        {"a" * 64: "run1.mp4", "b" * 64: "run2.mp4"},
        target_sec=15.0,
        model="claude-sonnet-4-5",
        sheets=sheets,
        client=client,
    )
    assert mix.title == "Two runs, one reel" and not mix.fallback
    assert usage.input_tokens == 1000
    blocks = client.calls[0]["messages"][0]["content"]
    assert len([b for b in blocks if b.get("type") == "image"]) == 2
    # One JSON text block per moment + the intro.
    texts = [b for b in blocks if b.get("type") == "text"]
    assert len(texts) == len(pool) + 1
    assert "Target duration: 15s" in texts[0]["text"]


async def test_sequence_mix_failure_falls_back():
    pool, analyses = _pool_and_analyses()
    client = _FakeSequencer(raise_exc=ValueError("boom"))
    mix, usage = await sequence_mix(
        pool, analyses, {}, target_sec=20.0, model="m", client=client
    )
    assert mix.fallback and mix.shots
    assert usage.input_tokens == 0


async def test_sequence_mix_direction_prompt_reaches_system():
    pool, analyses = _pool_and_analyses()
    payload = {
        "sequence": [{"moment_id": "m-a0"}, {"moment_id": "m-a1"}, {"moment_id": "m-b0"}],
        "title": "t", "hook": "h", "suggested_mood": "calm", "content_style": "chill",
    }
    client = _FakeSequencer(payload)
    await sequence_mix(
        pool, analyses, {}, target_sec=15.0, prompt="only the jumps",
        model="m", client=client,
    )
    assert "only the jumps" in client.calls[0]["system"]
    assert "USER DIRECTION" in client.calls[0]["system"]
