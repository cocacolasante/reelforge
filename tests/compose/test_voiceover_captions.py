"""Voiceover captions: placement, suppression of footage captions, cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.analysis.transcribe import ensure_take_transcript
from reelforge_core.compose.captions import build_captions
from reelforge_core.compose.clips import ClipInfo
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    VoiceoverTake,
)
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _vclip(asset_id: str, in_ts: float, out_ts: float) -> ClipInfo:
    return ClipInfo(
        path=Path("/tmp/c.mp4"), scene_index=-1, in_ts=in_ts, out_ts=out_ts,
        duration=out_ts - in_ts, has_audio=True, effects_applied=[], asset_id=asset_id,
    )


def _transcript(words: list[tuple[float, float, str]]) -> Transcript:
    ws = [TranscriptWord(start=a, end=b, word=f" {t}", probability=0.9) for a, b, t in words]
    return Transcript(language="en", language_probability=1.0, duration=ws[-1].end,
                      segments=[TranscriptSegment(start=ws[0].start, end=ws[-1].end, text="", words=ws)])


def _dialogues(path: Path) -> list[str]:
    return [l for l in path.read_text().splitlines() if l.startswith("Dialogue: 0,")]


def test_voiceover_words_land_at_take_offset(tmp_path: Path):
    analysis = _analysis([_scene(0, 0.0, 30.0)], None)  # footage has no speech
    take = VoiceoverTake(id="t", asset_id="vo", start_sec=4.0, duration_sec=3.0)
    out = build_captions(
        _reel([0], 0.0, 30.0), analysis,
        ComposeConfig(captions=CaptionStyle(mode="static")), tmp_path,
        clips=[_vclip(analysis.asset_id, 0.0, 30.0)],
        voiceover_captions=[(take, _transcript([(0.5, 0.9, "hello"), (1.0, 1.4, "there")]))],
    )
    lines = _dialogues(out)
    assert len(lines) == 1
    assert lines[0].split(",")[1] == "0:00:04.50" and lines[0].split(",")[2] == "0:00:05.40"
    assert lines[0].endswith("hello there")


def test_voiceover_captions_suppress_footage_words_under_take(tmp_path: Path):
    footage = _analysis(
        [_scene(0, 0.0, 30.0)],
        [TranscriptWord(start=5.0, end=5.4, word=" footage", probability=0.9),
         TranscriptWord(start=20.0, end=20.4, word=" later", probability=0.9)],
    )
    take = VoiceoverTake(id="t", asset_id="vo", start_sec=4.0, duration_sec=3.0)
    out = build_captions(
        _reel([0], 0.0, 30.0), footage,
        ComposeConfig(captions=CaptionStyle(mode="static"), speech_safe_cuts=False), tmp_path,
        clips=[_vclip(footage.asset_id, 0.0, 30.0)],
        voiceover_captions=[(take, _transcript([(0.2, 2.5, "narration")]))],
    )
    text = out.read_text()
    assert "narration" in text
    assert "footage" not in text, "footage word at 5s sits under the 4-6.5s take and must yield"
    assert "later" in text, "footage words outside the take still caption"


def test_muted_take_contributes_no_captions(tmp_path: Path):
    analysis = _analysis([_scene(0, 0.0, 30.0)], None)
    take = VoiceoverTake(id="t", asset_id="vo", start_sec=1.0, duration_sec=2.0, muted=True)
    out = build_captions(
        _reel([0], 0.0, 30.0), analysis, ComposeConfig(captions=CaptionStyle(mode="static")),
        tmp_path, clips=[_vclip(analysis.asset_id, 0.0, 30.0)],
        voiceover_captions=[(take, _transcript([(0.1, 0.5, "hidden")]))],
    )
    assert "hidden" not in out.read_text()


def test_voiceover_karaoke_events(tmp_path: Path):
    analysis = _analysis([_scene(0, 0.0, 30.0)], None)
    take = VoiceoverTake(id="t", asset_id="vo", start_sec=2.0, duration_sec=3.0)
    out = build_captions(
        _reel([0], 0.0, 30.0), analysis, ComposeConfig(captions=CaptionStyle(mode="karaoke")),
        tmp_path, clips=[_vclip(analysis.asset_id, 0.0, 30.0)],
        voiceover_captions=[(take, _transcript([(0.0, 0.4, "big"), (0.5, 0.9, "air")]))],
    )
    lines = _dialogues(out)
    assert len(lines) == 2
    assert "big" in lines[0] and "air" in lines[0]
    assert lines[0].split(",")[1] == "0:00:02.00"


def test_words_past_reel_end_are_dropped(tmp_path: Path):
    analysis = _analysis([_scene(0, 0.0, 10.0)], None)
    take = VoiceoverTake(id="t", asset_id="vo", start_sec=8.0, duration_sec=5.0)
    out = build_captions(
        _reel([0], 0.0, 10.0), analysis, ComposeConfig(captions=CaptionStyle(mode="static")),
        tmp_path, clips=[_vclip(analysis.asset_id, 0.0, 10.0)],
        voiceover_captions=[(take, _transcript([(0.5, 0.9, "inside"), (3.5, 4.0, "outside")]))],
    )
    text = out.read_text()
    assert "inside" in text and "outside" not in text


def test_ensure_take_transcript_caches_by_model_and_mtime(tmp_path: Path):
    take_file = tmp_path / "take.webm"
    take_file.write_bytes(b"x")
    calls: list[str] = []

    def fake(path: Path, model: str) -> Transcript:
        calls.append(model)
        return _transcript([(0.0, 0.3, "hi")])

    t1 = ensure_take_transcript(take_file, "vo1", tmp_path, "base.en", transcriber=fake)
    t2 = ensure_take_transcript(take_file, "vo1", tmp_path, "base.en", transcriber=fake)
    assert t1 is not None and t2 is not None and calls == ["base.en"]
    assert (tmp_path / "working" / "vo1" / "transcript.json").exists()
    ensure_take_transcript(take_file, "vo1", tmp_path, "small.en", transcriber=fake)
    assert calls == ["base.en", "small.en"]
    ensure_take_transcript(take_file, "vo2", tmp_path, "base.en", transcriber=lambda p, m: None)
    raw = json.loads((tmp_path / "working" / "vo2" / "transcript.json").read_text())
    assert raw == {"transcript": None}
    assert ensure_take_transcript(take_file, "vo2", tmp_path, "base.en",
                                  transcriber=lambda p, m: pytest.fail("must not rerun")) is None
