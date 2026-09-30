"""Pro-editing CP9: automatic B-roll for scene-mode reels."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.broll.suggest import program_duration, shot_segments
from reelforge_core.compose.autobroll import (
    auto_budget,
    plan_auto_broll,
    timeline_from_clips,
    to_layers,
    wants_auto_broll,
)
from reelforge_core.compose.clips import ClipInfo
from reelforge_core.models import BrollSource, ComposeConfig, TranscriptWord
from tests.compose.test_speech_snap import _analysis, _scene

A, B, P = "a" * 64, "b" * 64, "c" * 64


def _clip(i, s, e, *, asset=A, speed=1.0) -> ClipInfo:
    return ClipInfo(path=Path(f"/c{i}.mp4"), scene_index=0, in_ts=s, out_ts=e,
                    duration=(e - s) / speed, has_audio=True, effects_applied=[],
                    asset_id=asset, speed=speed)


SOURCES = [
    BrollSource(asset_id=A, kind="video", filename="talk.mp4", path="/data/uploads/a.mp4"),
    BrollSource(asset_id=B, kind="video", filename="demo.mp4", path="/data/uploads/b.mp4"),
    BrollSource(asset_id=P, kind="photo", filename="board.jpg", path="/data/uploads/c.jpg"),
]


def test_budget_scales_with_length():
    assert auto_budget(30.0) == 3  # ~1 per 8s on shorts
    assert auto_budget(60.0) == 7
    assert auto_budget(600.0) == 30  # ~1 per 20s on long-form, cap grows


def test_gating():
    on = ComposeConfig(broll_sources=SOURCES)
    assert wants_auto_broll(on, "talking_head", 0.8)
    assert not wants_auto_broll(on, "hype", 0.8)  # never over action
    assert not wants_auto_broll(on, "talking_head", 0.1)  # nothing said to match
    assert not wants_auto_broll(ComposeConfig(), "talking_head", 0.8)  # nothing to cut to
    assert not wants_auto_broll(on.model_copy(update={"smart_mode": False}), "classic", 0.8)
    assert wants_auto_broll(on.model_copy(update={"auto_broll": "on"}), "hype", 0.0)
    assert not wants_auto_broll(on.model_copy(update={"auto_broll": "off"}), "talking_head", 1.0)


def test_rendered_clips_map_like_the_editor():
    clips = [_clip(0, 10.0, 14.0), _clip(1, 20.0, 26.0), _clip(2, 30.0, 33.0, speed=0.5)]
    transitions = [("cut", 0.04), ("fade", 0.4)]
    tl = timeline_from_clips(clips, transitions, A)
    # The editor's placement: shot 1 starts where the cut into it lands.
    segs = shot_segments(tl)
    assert segs[1][1] == pytest.approx(4.0 - 0.04)
    assert program_duration(tl) == pytest.approx(4 + 6 + 6 - 0.04 - 0.4)
    assert tl.shots[2].speed == 0.5 and tl.shots[-1].transition_after is None


def test_to_layers_resolves_paths_and_drops_unknowns():
    sugs = [
        {"id": "sug-1", "kind": "video", "asset_id": B, "start_sec": 3.0, "end_sec": 6.0,
         "in_ts": 12.0, "mode": "full", "reason": "shows the scraper"},
        {"id": "sug-2", "kind": "photo", "asset_id": P, "start_sec": 12.0, "end_sec": 14.0},
        {"id": "sug-3", "kind": "video", "asset_id": "gone", "start_sec": 20.0, "end_sec": 22.0},
    ]
    layers = to_layers(sugs, SOURCES)
    assert [(ly.id, ly.path, ly.kind) for ly in layers] == [
        ("auto-1", "/data/uploads/b.mp4", "video"), ("auto-2", "/data/uploads/c.jpg", "photo")]


def _talky_analysis():
    words = [TranscriptWord(start=0.2 + i * 0.4, end=0.5 + i * 0.4, word=f" word{i}", probability=1)
             for i in range(60)]
    return _analysis([_scene(0, 0.0, 30.0)], words)


@pytest.mark.asyncio
async def test_plan_is_stamped_and_failures_mean_no_broll(tmp_path):
    analysis = _talky_analysis()
    working = tmp_path / "working"
    wd_b = working / B
    wd_b.mkdir(parents=True)
    other = _analysis([_scene(0, 0.0, 30.0)], None).model_copy(update={"asset_id": B})
    (wd_b / "analysis.json").write_text(other.model_dump_json())
    cfg = ComposeConfig(broll_sources=SOURCES[:2])
    clips = [_clip(0, 0.0, 12.0, asset=analysis.asset_id), _clip(1, 12.0, 24.0, asset=analysis.asset_id)]

    class Client:
        calls = 0

        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            Client.calls += 1
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name="record_broll", input={"suggestions": [
                    {"candidate_id": "v1", "start_sec": 4.0, "end_sec": 7.0, "reason": "x"}]})],
                stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    first = await plan_auto_broll(clips, [("cut", 0.04)], analysis, cfg,
                                  reel_dir=tmp_path, working_root=working, client=Client())
    stamp = json.loads((tmp_path / "auto_broll.json").read_text())
    assert stamp["version"] == "a1" and Client.calls == 1
    again = await plan_auto_broll(clips, [("cut", 0.04)], analysis, cfg,
                                  reel_dir=tmp_path, working_root=working, client=Client())
    assert Client.calls == 1 and [ly.model_dump() for ly in again] == [ly.model_dump() for ly in first]
    # Default client is blocked in tests; a changed cut misses the stamp -> [].
    shorter = [clips[0]]
    assert await plan_auto_broll(shorter, [], analysis, cfg, reel_dir=tmp_path,
                                 working_root=working) == []
