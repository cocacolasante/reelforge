"""B-roll picture layers: geometry, window clamping, the overlay graph, and a
real-ffmpeg render checking what's on screen inside and outside each layer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reelforge_core.compose.layers import LayerInput, layer_box, layer_fade, layer_window
from reelforge_core.models import PictureLayer, ReelTimeline, TimelineShot
from tests.compose.test_audio_controls import _clip, _plan
from tests.compose.test_compose_integration import synth_source  # noqa: F401 (fixture)


def _layer(**kw) -> PictureLayer:
    base = {"kind": "video", "asset_id": "b", "start_sec": 2.0, "end_sec": 5.0}
    return PictureLayer(**{**base, **kw})


# ---- pure helpers ------------------------------------------------------------


def test_layer_box_full_and_pip_corners():
    assert layer_box(_layer(), 1080, 1920) == (1080, 1920, 0, 0)
    w, h, x, y = layer_box(_layer(mode="pip", pip_corner="br", pip_scale=0.4), 1080, 1920)
    assert (w, h) == (432, 768)
    margin = round(1080 * 0.04)
    assert (x, y) == (1080 - 432 - margin, 1920 - 768 - margin)
    assert layer_box(_layer(mode="pip", pip_corner="tl"), 1080, 1920)[2:] == (margin, margin)
    assert w % 2 == 0 and h % 2 == 0


def test_layer_window_clamps_to_program_and_source():
    assert layer_window(_layer(), 30.0) == (2.0, 5.0)
    assert layer_window(_layer(start_sec=28.0, end_sec=35.0), 30.0) == (28.0, 30.0)
    assert layer_window(_layer(start_sec=29.95, end_sec=35.0), 30.0) is None
    # Only 1.5s of source left after in_ts.
    assert layer_window(_layer(in_ts=8.5), 30.0, source_sec=10.0) == (2.0, 3.5)
    assert layer_fade(_layer(fade_ms=900), 1.5) == 0.5


def test_timeline_layers_default_and_round_trip():
    tl = ReelTimeline(shots=[TimelineShot(kind="video", asset_id="a", in_ts=0, out_ts=5)])
    assert tl.layers == []
    tl2 = ReelTimeline(**{**tl.model_dump(), "layers": [_layer(mode="pip").model_dump()]})
    assert ReelTimeline(**tl2.model_dump()).layers[0].mode == "pip"


# ---- graph -------------------------------------------------------------------


def _li(start=2.0, end=5.0, x=0, y=0, fade=0.2) -> LayerInput:
    return LayerInput(path=Path("/tmp/layer.mp4"), start=start, end=end, x=x, y=y, fade=fade)


def test_no_layers_graph_unchanged():
    base = _plan([_clip(0), _clip(1)])
    from reelforge_core.compose.graph_builder import build_final_command

    same = build_final_command(
        clips=[_clip(0), _clip(1)],
        analysis=base_analysis(),
        music_path=None,
        captions_path=None,
        config=base_config(),
        output_path=Path("/tmp/out.mp4"),
        layers=[],
    )
    assert same.filter_complex == base.filter_complex
    assert "overlay" not in base.filter_complex


def base_analysis():
    from tests.compose.test_speech_snap import _analysis, _scene

    return _analysis([_scene(0, 0, 30)], None)


def base_config():
    from reelforge_core.models import CaptionStyle, ComposeConfig

    return ComposeConfig(captions=CaptionStyle(mode="off"))


def test_layers_overlay_after_crossfades_before_grade():
    from reelforge_core.compose.graph_builder import build_final_command

    plan = build_final_command(
        clips=[_clip(0), _clip(1)],
        analysis=base_analysis(),
        music_path=None,
        captions_path=None,
        config=base_config(),
        output_path=Path("/tmp/out.mp4"),
        voiceovers=[(Path("/tmp/t.webm"), 0.0, 1.0)],
        layers=[_li(), _li(start=8.0, end=9.0, x=40, y=60, fade=0.0)],
    )
    fc = plan.filter_complex
    # Inputs: 2 clips, 1 take, then the 2 layers.
    assert plan.args.count("-i") == 5
    assert "[3:v]format=yuva420p,setpts=PTS-STARTPTS+2.000/TB" in fc
    assert "fade=t=in:st=2.000:d=0.200:alpha=1" in fc and "fade=t=out:st=4.800" in fc
    assert "[4:v]format=yuva420p,setpts=PTS-STARTPTS+8.000/TB[ly1]" in fc  # no fade
    assert "overlay=x=40:y=60:eof_action=pass" in fc
    assert fc.index("xfade") < fc.index("overlay") < fc.index("unsharp")
    # The program length is unchanged by layers.
    assert plan.mezzanine_duration_sec == pytest.approx(19.6)


# ---- real render -------------------------------------------------------------


def _rgb_at(video: Path, t: float, px: tuple[int, int]) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=16:9", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    x, y = px
    i = (y * 16 + x) * 3
    return raw[i], raw[i + 1], raw[i + 2]


def _dominant(rgb: tuple[int, int, int]) -> str:
    return ("red", "green", "blue")[max(range(3), key=lambda k: rgb[k])]


@pytest.mark.asyncio
async def test_compose_renders_full_and_pip_layers(
    synth_source: Path, isolated_data_dir: Path, tmp_path: Path
) -> None:
    from reelforge_core import probe
    from reelforge_core.compose import compose
    from reelforge_core.models import CaptionStyle, ComposeConfig, EffectsConfig, TransitionStyle
    from tests.compose.test_compose_integration import _make_analysis_and_reel

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    photo = tmp_path / "green.png"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=400x300",
         "-frames:v", "1", str(photo)],
        check=True,
    )
    asset = probe(synth_source)  # red 0-10s, green 10-20s, blue 20-30s
    analysis, reel = _make_analysis_and_reel(asset.id)
    timeline = ReelTimeline(
        shots=[TimelineShot(kind="video", asset_id=asset.id, path=str(synth_source), in_ts=0.0, out_ts=10.0)],
        layers=[
            # Full-frame blue cutaway 2-5s.
            PictureLayer(kind="video", asset_id=asset.id, path=str(synth_source),
                         start_sec=2.0, end_sec=5.0, in_ts=21.0, fade_ms=0),
            # Green photo picture-in-picture, top-left, 6-9s.
            PictureLayer(kind="photo", asset_id="photo", path=str(photo), start_sec=6.0,
                         end_sec=9.0, mode="pip", pip_corner="tl", pip_scale=0.4, fade_ms=0),
        ],
    )
    config = ComposeConfig(
        aspect="16:9",
        video_preset="ultrafast",
        no_music=True,
        beat_sync=False,
        captions=CaptionStyle(mode="off"),
        effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False, lut=None),
        timeline=timeline,
        smart_mode=False,
        transition=TransitionStyle(kind="fade", duration_sec=0.4),
    )
    manifest = await compose(asset, reel, analysis, config)
    mezz = Path(manifest.mezzanine_path)
    assert manifest.duration_sec == pytest.approx(10.0, abs=0.1)

    center, corner = (8, 4), (1, 1)
    assert _dominant(_rgb_at(mezz, 1.0, center)) == "red"
    assert _dominant(_rgb_at(mezz, 3.5, center)) == "blue"  # full cutaway
    assert _dominant(_rgb_at(mezz, 5.6, center)) == "red"  # layer ended
    assert _dominant(_rgb_at(mezz, 7.5, corner)) == "green"  # PiP box
    assert _dominant(_rgb_at(mezz, 7.5, (13, 7))) == "red"  # outside the box
    assert _dominant(_rgb_at(mezz, 9.6, corner)) == "red"
