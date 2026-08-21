"""Editable timeline: per-cut transitions, overlays, multi-source captions."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.captions import build_captions, has_dialogue
from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.graph_builder import (
    _xfade_offsets,
    build_final_command,
    resolve_transitions,
)
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    ReelTimeline,
    TextOverlay,
    TimelineShot,
    TransitionStyle,
    TranscriptWord,
)
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _vclip(idx: int, asset_id: str, in_ts: float, out_ts: float) -> ClipInfo:
    return ClipInfo(
        path=Path(f"/tmp/clip_{idx:04d}.mp4"),
        scene_index=-1,
        in_ts=in_ts,
        out_ts=out_ts,
        duration=out_ts - in_ts,
        has_audio=True,
        effects_applied=[],
        asset_id=asset_id,
    )


# ---- models ----------------------------------------------------------------


def test_timeline_shot_durations():
    v = TimelineShot(kind="video", asset_id="a", in_ts=10.0, out_ts=25.5)
    p = TimelineShot(kind="photo", asset_id="b", duration_sec=4.0)
    assert v.duration == pytest.approx(15.5)
    assert p.duration == 4.0
    tl = ReelTimeline(shots=[v, p])
    assert tl.total_duration == pytest.approx(19.5)


def test_compose_config_round_trips_timeline():
    cfg = ComposeConfig(
        timeline=ReelTimeline(
            shots=[TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5)],
            overlays=[TextOverlay(text="hi", start_sec=0, end_sec=2)],
        )
    )
    again = ComposeConfig(**cfg.model_dump())
    assert again.timeline is not None
    assert again.timeline.overlays[0].text == "hi"


# ---- per-cut transitions ---------------------------------------------------


def test_xfade_offsets_accept_per_cut_durations():
    # Clips [5, 6, 4]; cuts of 0.4 then 1.0 -> offsets [4.6, 5+6-0.4-1.0]
    assert _xfade_offsets([5.0, 6.0, 4.0], [0.4, 1.0]) == pytest.approx([4.6, 9.6])
    # Scalar form unchanged.
    assert _xfade_offsets([5.0, 6.0, 4.0], 0.4) == pytest.approx([4.6, 10.2])


def test_resolve_transitions_overrides_and_cut():
    cfg = ComposeConfig(transition=TransitionStyle(kind="fade", duration_sec=0.5))
    cuts = resolve_transitions(cfg, 4, [None, ("slideleft", 1.0), ("cut", 9.0)])
    assert cuts == [("fade", 0.5), ("slideleft", 1.0), ("fade", 0.04)]


def test_build_command_uses_per_cut_transitions():
    clips = [_vclip(0, "a", 0, 10), _vclip(1, "a", 10, 20), _vclip(2, "a", 20, 30)]
    plan = build_final_command(
        clips=clips,
        analysis=_analysis([_scene(0, 0, 30)], None),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(captions=CaptionStyle(mode="off")),
        output_path=Path("/tmp/out.mp4"),
        transitions=[("slideleft", 0.4), ("fadeblack", 1.2)],
    )
    fc = plan.filter_complex
    assert "transition=slideleft" in fc and "duration=0.400" in fc
    assert "transition=fadeblack" in fc and "duration=1.200" in fc
    assert plan.mezzanine_duration_sec == pytest.approx(30 - 0.4 - 1.2)


def test_build_command_rejects_wrong_transition_count():
    clips = [_vclip(0, "a", 0, 10), _vclip(1, "a", 10, 20)]
    with pytest.raises(ValueError):
        build_final_command(
            clips=clips,
            analysis=_analysis([_scene(0, 0, 30)], None),
            music_path=None,
            captions_path=None,
            config=ComposeConfig(),
            output_path=Path("/tmp/out.mp4"),
            transitions=[("fade", 0.4), ("fade", 0.4)],
        )


# ---- overlays --------------------------------------------------------------


def test_text_overlay_written_even_without_transcript(tmp_path: Path):
    scenes = [_scene(0, 0.0, 30.0)]
    analysis = _analysis(scenes, None)  # no transcript at all
    reel = _reel([0], 0.0, 30.0)
    out = build_captions(
        reel,
        analysis,
        ComposeConfig(),
        tmp_path,
        clips=[_vclip(0, "a", 0.0, 30.0)],
        overlays=[
            TextOverlay(
                text="Big Air!", start_sec=2.0, end_sec=5.0, position="top",
                font_size_px=96, color="&H0000FFFF", fade_ms=300,
            )
        ],
    )
    text = out.read_text()
    assert has_dialogue(out)
    line = next(l for l in text.splitlines() if l.startswith("Dialogue: 1,"))
    assert "0:00:02.00,0:00:05.00,Overlay" in line
    assert "\\an8" in line and "\\fad(300,300)" in line and "\\fs96" in line
    assert "\\c&H00FFFF&" in line
    assert line.rstrip().endswith("Big Air!")
    # An Overlay style is declared.
    assert "Style: Overlay," in text


def test_overlay_escapes_braces_and_newlines(tmp_path: Path):
    analysis = _analysis([_scene(0, 0.0, 10.0)], None)
    out = build_captions(
        _reel([0], 0.0, 10.0),
        analysis,
        ComposeConfig(),
        tmp_path,
        clips=[_vclip(0, "a", 0.0, 10.0)],
        overlays=[TextOverlay(text="a {b}\nc", start_sec=0, end_sec=1)],
    )
    line = next(l for l in out.read_text().splitlines() if l.startswith("Dialogue: 1,"))
    assert line.endswith("a \\{b\\}\\Nc")


def test_empty_overlay_text_is_skipped(tmp_path: Path):
    analysis = _analysis([_scene(0, 0.0, 10.0)], None)
    out = build_captions(
        _reel([0], 0.0, 10.0),
        analysis,
        ComposeConfig(),
        tmp_path,
        clips=[_vclip(0, "a", 0.0, 10.0)],
        overlays=[TextOverlay(text="   ", start_sec=0, end_sec=1)],
    )
    assert not has_dialogue(out)


# ---- multi-source captions -------------------------------------------------


def test_captions_map_each_shot_to_its_own_transcript(tmp_path: Path):
    """Two shots from two assets whose source times OVERLAP: each caption
    must come from its own asset's transcript and land at the shot's
    mezzanine position."""
    a = _analysis(
        [_scene(0, 0.0, 30.0)],
        [TranscriptWord(start=2.0, end=2.5, word=" alpha", probability=0.9)],
    )
    b = _analysis(
        [_scene(0, 0.0, 30.0)],
        [TranscriptWord(start=2.0, end=2.5, word=" bravo", probability=0.9)],
    )
    a = a.model_copy(update={"asset_id": "A"})
    b = b.model_copy(update={"asset_id": "B"})
    clips = [_vclip(0, "A", 0.0, 10.0), _vclip(1, "B", 0.0, 10.0)]
    cfg = ComposeConfig(captions=CaptionStyle(mode="static"), speech_safe_cuts=False)
    out = build_captions(
        _reel([0], 0.0, 10.0),
        a,
        cfg,
        tmp_path,
        clips=clips,
        analyses={"A": a, "B": b},
        xfades=[0.5],
    )
    lines = [l for l in out.read_text().splitlines() if l.startswith("Dialogue: 0,")]
    assert len(lines) == 2
    # "alpha": shot 0 at mezz 2.0.  "bravo": shot 1 starts at 10 - 0.5 = 9.5, word at +2.0 = 11.5
    assert lines[0].split(",")[1] == "0:00:02.00" and lines[0].endswith("alpha")
    assert lines[1].split(",")[1] == "0:00:11.50" and lines[1].endswith("bravo")


def test_captions_honor_variable_xfades(tmp_path: Path):
    a = _analysis(
        [_scene(0, 0.0, 30.0)],
        [TranscriptWord(start=21.0, end=21.5, word=" late", probability=0.9)],
    )
    # Three shots of 10s from the same asset (0-10, 10-20, 20-30), cuts 0.4 then 2.0.
    clips = [_vclip(i, a.asset_id, i * 10.0, (i + 1) * 10.0) for i in range(3)]
    cfg = ComposeConfig(captions=CaptionStyle(mode="static"), speech_safe_cuts=False)
    out = build_captions(
        _reel([0], 0.0, 30.0), a, cfg, tmp_path, clips=clips, xfades=[0.4, 2.0]
    )
    line = next(l for l in out.read_text().splitlines() if l.startswith("Dialogue: 0,"))
    # shot 2 starts at 20 - 0.4 - 2.0 = 17.6; word at +1.0 => 18.6
    assert line.split(",")[1] == "0:00:18.60"


def test_shot_from_unanalyzed_source_has_no_captions(tmp_path: Path):
    a = _analysis(
        [_scene(0, 0.0, 30.0)],
        [TranscriptWord(start=1.0, end=1.5, word=" hey", probability=0.9)],
    )
    clips = [_vclip(0, "UNANALYZED", 0.0, 10.0), _vclip(1, a.asset_id, 0.0, 10.0)]
    cfg = ComposeConfig(captions=CaptionStyle(mode="static"), speech_safe_cuts=False)
    out = build_captions(
        _reel([0], 0.0, 10.0), a, cfg, tmp_path, clips=clips,
        analyses={"UNANALYZED": None, a.asset_id: a}, xfades=[0.4],
    )
    lines = [l for l in out.read_text().splitlines() if l.startswith("Dialogue: 0,")]
    assert len(lines) == 1 and lines[0].endswith("hey")
    assert lines[0].split(",")[1] == "0:00:10.60"  # 10 - 0.4 + 1.0
