"""Long-form videos across clips: section mining, shot caps, the long-form
sequencer note, and hierarchical rendering for very long timelines."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.compose import pipeline
from reelforge_core.mixes import mining
from reelforge_core.mixes.planner import MAX_MIX_SHOTS
from reelforge_core.mixes.sequencer import sequence_mix


def test_long_targets_mine_sections_not_highlights():
    assert mining.moment_bounds_for(60) == mining.SHORT_MIX_BOUNDS
    assert mining.moment_bounds_for(200) == mining.LONG_MIX_BOUNDS
    assert mining.moment_bounds_for(480) == mining.LONG_FORM_BOUNDS
    assert mining.dedupe_overlap_for(480) == mining.LONG_FORM_DEDUPE_OVERLAP
    assert mining.dedupe_overlap_for(120) == mining.DEDUPE_OVERLAP


def test_short_clips_still_contribute_sections():
    assert mining.fit_bounds_to_clip((20.0, 90.0), 26.2) == (20.0, 90.0)
    assert mining.fit_bounds_to_clip((20.0, 90.0), 15.0) == (9.0, 15.0)
    assert mining.fit_bounds_to_clip((2.0, 8.0), 1.0) == (1.0, 1.0)


def test_shot_caps_agree_and_allow_long_videos():
    from apps.api.routers.reels import MAX_TIMELINE_SHOTS

    assert MAX_MIX_SHOTS == MAX_TIMELINE_SHOTS >= 300


def test_mix_api_accepts_thirty_minutes():
    from pydantic import ValidationError

    from apps.api.routers.mixes import MixCreate

    assert MixCreate(target_duration_sec=1800).target_duration_sec == 1800
    with pytest.raises(ValidationError):
        MixCreate(target_duration_sec=1801)


class _CapturingClient:
    def __init__(self):
        self.kwargs: dict = {}
        self.messages = self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        block = SimpleNamespace(type="tool_use", name="record_mix", input={"sequence": []})
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=1))


async def test_sequencer_tells_the_model_it_is_building_long_form():
    long_client, short_client = _CapturingClient(), _CapturingClient()
    await sequence_mix([], {}, {}, target_sec=480, model="m", client=long_client)
    await sequence_mix([], {}, {}, target_sec=60, model="m", client=short_client)
    assert "LONG-FORM VIDEO" in long_client.kwargs["system"]
    assert "about 8 minutes" in long_client.kwargs["system"]
    assert "LONG-FORM VIDEO" not in short_client.kwargs["system"]


async def test_render_hierarchy_chunks_until_the_final_pass_is_small(monkeypatch):
    calls: list[tuple[int, int]] = []

    async def fake_chunks(clips, transitions, analysis, config, reel_dir, log_file, progress, level=0):
        calls.append((level, len(clips)))
        slices = pipeline._chunk_slices(len(clips))
        parts = [f"L{level}-{i}" for i in range(len(slices))]
        boundary = [transitions[b - 1] for _, b in slices if b - 1 < len(transitions)]
        return parts, boundary

    monkeypatch.setattr(pipeline, "_render_chunks", fake_chunks)
    clips = [f"c{i}" for i in range(60)]
    transitions = [("cut", 0.04)] * 59
    out, bounds = await pipeline._render_hierarchy(
        clips, transitions, None, None, Path("/tmp"), Path("/tmp/log"), None
    )
    assert calls == [(0, 60), (1, 12)]  # 60 -> 12 parts -> 3 parts
    assert len(out) == 3 and len(bounds) == 2

    calls.clear()
    out, _ = await pipeline._render_hierarchy(
        clips[:6], transitions[:5], None, None, Path("/tmp"), Path("/tmp/log"), None
    )
    assert calls == [] and len(out) == 6  # small timelines render in one pass
