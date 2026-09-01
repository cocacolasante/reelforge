"""Edit Quality CP1: speed, punch-in, transition vocabulary, per-cut plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.beats import BeatGrid, beats_in_range
from reelforge_core.compose.clips import (
    ClipInfo,
    _atempo_chain,
    build_clip_command,
)
from reelforge_core.compose.graph_builder import (
    _TRANSITION_MAP,
    build_final_command,
    expand_transitions_for_photos,
    resolve_transitions,
)
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    TimelineShot,
    TransitionStyle,
)
from tests.compose.test_speech_snap import _analysis, _scene


def _vclip(idx: int, in_ts: float, out_ts: float, **kw) -> ClipInfo:
    speed = kw.pop("speed", 1.0)
    return ClipInfo(
        path=Path(f"/tmp/clip_{idx:04d}.mp4"),
        scene_index=-1,
        in_ts=in_ts,
        out_ts=out_ts,
        duration=(out_ts - in_ts) / speed,
        has_audio=True,
        effects_applied=[],
        asset_id="a",
        speed=speed,
        **kw,
    )


def _pclip(idx: int, dur: float) -> ClipInfo:
    return ClipInfo(
        path=Path(f"/tmp/clip_{idx:04d}.mp4"),
        scene_index=-1,
        in_ts=0,
        out_ts=0,
        duration=dur,
        has_audio=True,
        effects_applied=[],
        is_photo=True,
    )


# ---- speed -----------------------------------------------------------------


def test_atempo_chain_decomposition():
    assert _atempo_chain(1.5) == "atempo=1.5"
    assert _atempo_chain(4.0) == "atempo=2,atempo=2"
    assert _atempo_chain(3.0) == "atempo=2,atempo=1.5"
    assert _atempo_chain(0.25) == "atempo=0.5,atempo=0.5"
    # Every factor lands in atempo's supported [0.5, 2.0] band.
    for speed in (0.25, 0.4, 0.5, 0.75, 1.3, 2.0, 3.7, 4.0):
        factors = [float(p.split("=")[1]) for p in _atempo_chain(speed).split(",")]
        assert all(0.5 <= f <= 2.0 for f in factors)
        prod = 1.0
        for f in factors:
            prod *= f
        assert prod == pytest.approx(speed)


def test_clip_command_speed_golden():
    cmd = build_clip_command(
        source=Path("/src.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=10.0,
        out_ts=20.0,
        config=ComposeConfig(),
        has_audio=True,
        is_hdr=False,
        speed=2.0,
    )
    vf = cmd[cmd.index("-vf") + 1]
    af = cmd[cmd.index("-af") + 1]
    # setpts comes FIRST so the later fps filter yields constant frame rate.
    assert vf.startswith("setpts=PTS/2")
    assert vf.index("setpts") < vf.index("fps=")
    assert "atempo=2" in af
    # Output-side seek window scales by 1/speed (post-setpts timestamps).
    assert cmd[cmd.index("-ss") + 1] == "5.000"
    assert cmd[cmd.index("-to") + 1] == "10.000"


def test_clip_command_speed_one_is_unchanged():
    kw = dict(
        source=Path("/src.mp4"),
        out_path=Path("/out.mp4"),
        in_ts=10.0,
        out_ts=20.0,
        config=ComposeConfig(),
        has_audio=True,
        is_hdr=False,
    )
    assert build_clip_command(**kw) == build_clip_command(**kw, speed=1.0)
    assert "setpts" not in build_clip_command(**kw)[build_clip_command(**kw).index("-vf") + 1]


def test_timeline_shot_speed_scales_duration_and_mutes():
    s = TimelineShot(kind="video", asset_id="a", in_ts=0.0, out_ts=10.0, speed=2.0)
    assert s.duration == pytest.approx(5.0)
    assert s.effective_gain == 0.0  # v1: sped shots render muted
    assert TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=10).duration == 10.0


def test_timeline_shot_speed_bounds():
    with pytest.raises(Exception):
        TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5, speed=5.0)
    with pytest.raises(Exception):
        TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5, punch_in=2.0)


# ---- punch-in + Ken Burns craft --------------------------------------------


def _plan(clips, transitions):
    return build_final_command(
        clips=clips,
        analysis=_analysis([_scene(0, 0, 60)], None),
        music_path=None,
        captions_path=None,
        config=ComposeConfig(
            captions=CaptionStyle(mode="off"),
            effects=EffectsConfig(ken_burns_on_low_energy=False, unsharp=False, lut=None),
        ),
        output_path=Path("/tmp/out.mp4"),
        transitions=transitions,
    )


def test_static_punch_in_renders_centered_crop():
    clips = [_vclip(0, 0, 10, punch_in=1.3), _vclip(1, 10, 20)]
    fc = _plan(clips, [("fade", 0.4)]).filter_complex
    assert "crop=1080:1920:(iw-ow)/2:(ih-oh)/2" in fc
    assert "pow(" not in fc  # static: no drift expression


def test_animated_punch_in_drifts():
    clips = [_vclip(0, 0, 10, punch_in=1.3, punch_in_animated=True), _vclip(1, 10, 20)]
    fc = _plan(clips, [("fade", 0.4)]).filter_complex
    assert "pow(" in fc and "(iw-ow)*" in fc


def test_ken_burns_direction_rotates_and_eases():
    clips = [
        _vclip(0, 0, 10, force_ken_burns=True),
        _vclip(1, 10, 20, force_ken_burns=True),
    ]
    fc = _plan(clips, [("fade", 0.4)]).filter_complex
    # Both clips drift, eased (pow), with different directions: clip 0 uses
    # (1-inv) for x, clip 1 uses inv directly.
    assert fc.count("(iw-ow)*") == 2
    assert "x='(iw-ow)*(1-pow(" in fc
    assert "x='(iw-ow)*pow(" in fc


# ---- transition vocabulary -------------------------------------------------


def test_every_model_kind_maps_to_xfade():
    from typing import get_args

    from reelforge_core.models import TransitionStyle as TS

    kinds = get_args(TS.model_fields["kind"].annotation)
    for kind in kinds:
        assert kind in _TRANSITION_MAP, kind


def test_new_kinds_flow_through_resolve():
    cfg = ComposeConfig(transition=TransitionStyle(kind="smoothleft", duration_sec=0.5))
    cuts = resolve_transitions(cfg, 3, [None, ("circleopen", 0.8)])
    assert cuts == [("smoothleft", 0.5), ("circleopen", 0.8)]


# ---- per-cut preservation through photo interleave --------------------------


def test_expand_transitions_for_photos_preserves_video_cuts():
    # videos v0 v1 v2 with cuts [slideleft, circleopen]; photo inserted
    # between v1 and v2.
    clips = [_vclip(0, 0, 10), _vclip(1, 10, 20), _pclip(2, 3.0), _vclip(3, 20, 30)]
    out = expand_transitions_for_photos(
        clips, [("slideleft", 0.4), ("circleopen", 0.8)], ("fade", 0.4)
    )
    assert out == [("slideleft", 0.4), ("fade", 0.4), ("fade", 0.4)]


def test_expand_transitions_photo_at_edges():
    clips = [_pclip(0, 3.0), _vclip(1, 0, 10), _vclip(2, 10, 20), _pclip(3, 3.0)]
    out = expand_transitions_for_photos(clips, [("wiperight", 0.6)], ("fade", 0.4))
    assert out == [("fade", 0.4), ("wiperight", 0.6), ("fade", 0.4)]


# ---- beats helpers ---------------------------------------------------------


def test_beats_in_range_and_snap():
    grid = BeatGrid(bpm=120.0, phase_sec=0.25)  # beats every 0.5s from 0.25
    assert beats_in_range(grid, 1.0, 2.3) == [1.25, 1.75, 2.25]
    assert beats_in_range(grid, 0.0, 0.2) == []
    assert grid.snap(1.4) == pytest.approx(1.25)
    assert grid.snap(1.6) == pytest.approx(1.75)
