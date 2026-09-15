"""AI B-roll suggestions: timeline word mapping, the candidate catalog,
local validation, and the (faked) model call."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.broll.suggest import (
    Candidate,
    PhotoSource,
    VideoSource,
    collect_candidates,
    photo_thumbnail,
    program_duration,
    shot_segments,
    spoken_lines,
    suggest_broll,
    validate_suggestions,
)
from reelforge_core.models import (
    PictureLayer,
    ReelTimeline,
    SceneSemantics,
    TimelineShot,
    TransitionStyle,
)
from tests.compose.test_jumpcuts import _transcript, _w
from tests.compose.test_speech_snap import _analysis, _scene


def _shot(aid: str, a: float, b: float, **kw) -> TimelineShot:
    return TimelineShot(kind="video", asset_id=aid, in_ts=a, out_ts=b, **kw)


# ---- timeline mapping ----------------------------------------------------------


def test_shot_segments_mirror_preview_placement():
    tl = ReelTimeline(
        shots=[
            _shot("a", 0, 10, transition_after=TransitionStyle(kind="cut", duration_sec=0.04)),
            _shot("a", 20, 30),
            _shot("a", 40, 44),
        ]
    )
    starts = [round(s, 2) for _, s, _ in shot_segments(tl)]
    assert starts == [0.0, 9.96, 19.56]  # cut 0.04, then the 0.4 reel default
    assert program_duration(tl) == pytest.approx(23.56)


def test_spoken_lines_map_words_and_split_sentences():
    tr = _transcript([_w(1.0, 1.4, "Hello"), _w(1.5, 2.0, "world."), _w(2.1, 2.5, "Next"),
                      _w(21.0, 21.5, "later"), _w(41.0, 41.5, "fast")])
    tl = ReelTimeline(shots=[_shot("a", 0, 10), _shot("a", 20, 30), _shot("a", 40, 44, speed=2.0)])
    lines = spoken_lines(tl, {"a": tr})
    assert [ln.text for ln in lines] == ["Hello world.", "Next", "later"]  # sped shot is muted
    assert lines[0].start == pytest.approx(1.0)
    assert lines[2].start == pytest.approx(9.6 + 1.0)


# ---- catalog -----------------------------------------------------------------


def _video(aid: str, scenes, semantics=(), tmp: Path = Path("/nonexistent")) -> VideoSource:
    analysis = _analysis(list(scenes), None).model_copy(update={"semantics": list(semantics)})
    return VideoSource(aid, f"{aid}.mp4", analysis, tmp)


def test_collect_candidates_skips_main_track_scenes_and_balances():
    talking = _video("a", [_scene(0, 0, 10), _scene(1, 10, 20), _scene(2, 20, 40)],
                     [SceneSemantics(scene_index=1, summary="whiteboard sketch", tags=["a", "b", "c"],
                                     mood="neutral", has_speech=False, visual_energy="low")])
    broll = _video("b", [_scene(0, 0, 5), _scene(1, 5, 5.5)])
    tl = ReelTimeline(shots=[_shot("a", 0, 10), _shot("a", 20, 30)])
    cands = collect_candidates(tl, [talking, broll], [PhotoSource("p", "beach.jpg")])
    assert [(c.id, c.kind, c.asset_id) for c in cands] == [
        ("c1", "photo", "p"), ("c2", "video", "a"), ("c3", "video", "b"),
    ]
    assert cands[1].start == 10 and cands[1].summary == "whiteboard sketch"
    # Scene 2 of "a" is half on screen already; scene 1 of "b" is too short.
    assert len(collect_candidates(tl, [talking, broll], [], max_candidates=1)) == 1


# ---- validation ----------------------------------------------------------------


def test_validate_clamps_drops_and_sorts():
    cands = [
        Candidate(id="c1", kind="video", asset_id="b", filename="b.mp4", start=100.0, end=104.0),
        Candidate(id="c2", kind="photo", asset_id="p", filename="beach.jpg"),
    ]
    raw = [
        {"candidate_id": "zz", "start_sec": 1, "end_sec": 3, "reason": "x"},
        {"candidate_id": "c2", "start_sec": 29, "end_sec": 29.5, "reason": "late"},
        {"candidate_id": "c1", "start_sec": 2, "end_sec": 9, "source_offset_sec": 3,
         "mode": "pip", "reason": "r", "quote": "q"},
        {"candidate_id": "c2", "start_sec": 11, "end_sec": 13, "reason": "hits existing"},
        {"candidate_id": "c2", "start_sec": 5, "end_sec": 8, "reason": "hits c1"},
        {"candidate_id": "c1", "start_sec": "bad", "reason": "x"},
    ]
    out = validate_suggestions(raw, cands, program_sec=30.0, existing=[(10.0, 12.0)])
    assert [(s["asset_id"], s["start_sec"], s["end_sec"]) for s in out] == [
        ("b", 2.0, 6.0),  # capped at the scene's 4s
        ("p", 28.5, 30.0),  # stretched to 1.5s, pulled inside the reel
    ]
    assert out[0]["in_ts"] == 100.0 and out[0]["mode"] == "pip" and out[0]["quote"] == "q"
    # Every suggestion is a valid PictureLayer once the extras are stripped.
    for s in out:
        PictureLayer(**{k: v for k, v in s.items() if k not in ("filename", "reason", "quote")})


# ---- the call ------------------------------------------------------------------


class _FakeClient:
    def __init__(self, suggestions):
        self.suggestions = suggestions
        self.calls = 0
        self.kwargs: dict = {}
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        block = SimpleNamespace(type="tool_use", name="record_broll", input={"suggestions": self.suggestions})
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=900, output_tokens=120))


def _talking_timeline_and_sources():
    tr = _transcript([_w(1.0, 1.4, "We"), _w(1.5, 2.0, "surfed."), _w(3.0, 3.5, "Then"), _w(3.6, 4.0, "built.")])
    talking = _video("a", [_scene(0, 0, 10)])
    tl = ReelTimeline(shots=[_shot("a", 0, 10)])
    return tl, talking, {"a": tr}


async def test_suggest_broll_calls_model_and_validates():
    tl, talking, transcripts = _talking_timeline_and_sources()
    client = _FakeClient([{"candidate_id": "c1", "start_sec": 1.0, "end_sec": 3.0, "reason": "surf"}])
    res = await suggest_broll(tl, [talking], [PhotoSource("p", "surf.jpg")], transcripts,
                              model="m", prompt="use the surf photo", client=client)
    assert client.calls == 1 and client.kwargs["tool_choice"]["name"] == "record_broll"
    assert [s["asset_id"] for s in res.suggestions] == ["p"]
    assert res.usage.input_tokens == 900 and res.note is None
    assert "use the surf photo" in client.kwargs["messages"][0]["content"][-1]["text"]


async def test_suggest_broll_without_candidates_skips_the_model():
    tl, talking, transcripts = _talking_timeline_and_sources()
    client = _FakeClient([])
    res = await suggest_broll(tl, [talking], [], transcripts, model="m", client=client)
    assert client.calls == 0 and res.suggestions == [] and "upload" in (res.note or "")


def test_photo_thumbnail_renders_small_jpeg(tmp_path: Path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    src = tmp_path / "big.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=1600x1200",
                    "-frames:v", "1", str(src)], check=True)
    thumb = photo_thumbnail(src, tmp_path / "wd")
    assert thumb is not None and thumb.exists() and thumb.stat().st_size < src.stat().st_size
    assert photo_thumbnail(tmp_path / "missing.png", tmp_path / "wd2") is None
