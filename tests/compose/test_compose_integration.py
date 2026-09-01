"""End-to-end compose integration — runs real FFmpeg on a synthesized fixture.

Slow-ish but essential: this is the first phase that actually writes video, and
unit tests alone can't prove the filter graph works. Builds a small 3-scene
clip + a canned analysis/reel, runs `compose`, probes the output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reelforge_core import probe
from reelforge_core.compose import compose
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    RankedReel,
    ReelScores,
    REELFORGE_VERSION,
    Scene,
    SceneSemantics,
    TransitionStyle,
)


def _ffprobe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _build_3_scene_source(tmp: Path) -> Path:
    """Concat 3 × 10s solid-color clips with tones into one source MP4."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    parts = []
    for i, color in enumerate(["red", "green", "blue"]):
        p = tmp / f"p{i}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=640x360:d=10:r=25",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={220 + i * 80}:duration=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(p),
            ],
            check=True,
            capture_output=True,
        )
        parts.append(p)
    concat_list = tmp / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p}'" for p in parts))
    src = tmp / "src.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    return src


@pytest.fixture(scope="module")
def synth_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_3_scene_source(tmp_path_factory.mktemp("src"))


def _make_analysis_and_reel(asset_id: str) -> tuple[AnalysisReport, RankedReel]:
    scenes = [
        Scene(index=i, start_sec=i * 10.0, end_sec=(i + 1) * 10.0,
              start_frame=i * 250, end_frame=(i + 1) * 250,
              thumbnail_path=f"thumbs/scene_{i:04d}.jpg")
        for i in range(3)
    ]
    sems = [
        SceneSemantics(
            scene_index=i,
            summary=f"scene {i}",
            tags=["a", "b", "c"],
            mood="neutral",
            has_speech=False,
            visual_energy="medium",
        )
        for i in range(3)
    ]
    analysis = AnalysisReport(
        asset_id=asset_id,
        source_path="/tmp/src.mp4",
        duration=30.0,
        width=640,
        height=360,
        fps=25.0,
        has_audio=True,
        config=AnalysisConfig(),
        scenes=scenes,
        transcript=None,
        loudness=[],
        semantics=sems,
        created_at="2026-01-01T00:00:00+00:00",
        elapsed_sec=1.0,
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage={},
    )
    reel = RankedReel(
        candidate_id="cand" + "0" * 12,
        scene_indices=[0, 1, 2],
        start_sec=0.0,
        end_sec=30.0,
        duration_sec=30.0,
        title="Test reel",
        hook="A hook",
        justification="because",
        scores=ReelScores(
            narrative_coherence=70,
            hook_strength=70,
            emotional_payoff=70,
            standalone_clarity=70,
        ),
        overall=70.0,
        rank=1,
        suggested_mood="neutral",
    )
    return analysis, reel


@pytest.mark.asyncio
async def test_compose_produces_playable_mezzanine(
    synth_source: Path,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Route music lib to the bundled-asset paths baked in the image.
    # This test runs inside the worker image where /app/assets/music exists.
    asset = probe(synth_source)
    # Make the asset's source_path match what probe saw so pipeline can find it.
    analysis, reel = _make_analysis_and_reel(asset.id)
    config = ComposeConfig(
        aspect="9:16",
        target_fps=30,
        video_preset="ultrafast",  # speed over quality for tests
        captions=CaptionStyle(mode="off"),
        transition=TransitionStyle(kind="fade", duration_sec=0.4),
        effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False),
    )

    manifest = await compose(asset, reel, analysis, config)

    mezz = Path(manifest.mezzanine_path)
    assert mezz.exists() and mezz.stat().st_size > 0
    probe_data = _ffprobe(mezz)
    streams = probe_data.get("streams", [])
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert int(video["width"]) == 1080
    assert int(video["height"]) == 1920
    assert audio is not None, "expected audio stream (voice + music mix)"
    duration = float(probe_data["format"]["duration"])
    # Expected: 30 - 2*0.4 = 29.2s, tolerate small container drift
    assert abs(duration - 29.2) < 0.5


@pytest.mark.asyncio
async def test_compose_deterministic_byte_identical(
    synth_source: Path,
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = probe(synth_source)
    analysis, reel = _make_analysis_and_reel(asset.id)
    config = ComposeConfig(
        aspect="9:16",
        target_fps=30,
        video_preset="ultrafast",
        captions=CaptionStyle(mode="off"),
        effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False),
        no_music=True,  # strip music so determinism depends only on clips + render
    )
    first = await compose(asset, reel, analysis, config)
    first_path = Path(first.mezzanine_path)
    first_bytes = first_path.read_bytes()
    first_path.unlink()

    second = await compose(asset, reel, analysis, config)
    second_bytes = Path(second.mezzanine_path).read_bytes()
    # Identical config + source → identical mezzanine
    assert first_bytes == second_bytes


@pytest.mark.asyncio
async def test_compose_timeline_with_speed_punchin_and_mixed_cuts(
    synth_source: Path,
    isolated_data_dir: Path,
) -> None:
    """CP1 integration: real ffmpeg render with a 2x shot, a punched-in shot,
    and per-cut transition variety from the new vocabulary."""
    from reelforge_core.models import ReelTimeline, TimelineShot

    asset = probe(synth_source)
    analysis, reel = _make_analysis_and_reel(asset.id)
    timeline = ReelTimeline(
        shots=[
            TimelineShot(
                kind="video",
                asset_id=asset.id,
                path=str(synth_source),
                in_ts=0.0,
                out_ts=6.0,
                speed=2.0,  # -> 3s of mezzanine
                transition_after=TransitionStyle(kind="smoothleft", duration_sec=0.4),
            ),
            TimelineShot(
                kind="video",
                asset_id=asset.id,
                path=str(synth_source),
                in_ts=10.0,
                out_ts=16.0,
                punch_in=1.3,
            ),
        ]
    )
    config = ComposeConfig(
        aspect="9:16",
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
    assert mezz.exists() and mezz.stat().st_size > 0
    probe_data = _ffprobe(mezz)
    dur = float(probe_data["format"]["duration"])
    # 3.0 (sped) + 6.0 - 0.4 xfade = 8.6s.
    assert dur == pytest.approx(8.6, abs=0.3)


@pytest.mark.asyncio
async def test_compose_jump_cuts_remove_silence(
    synth_source: Path,
    isolated_data_dir: Path,
) -> None:
    """CP2 integration: jump_cuts="on" splits a shot around dead air and the
    render loses exactly the removed silence."""
    from reelforge_core.models import Transcript, TranscriptSegment, TranscriptWord

    def _w(t0: float, t1: float) -> TranscriptWord:
        return TranscriptWord(start=t0, end=t1, word="w", probability=0.9)

    # Speech 0.2-3.0 and 6.0-9.8 inside scene 0; scene 1 continuous 10.2-19.8.
    words = []
    t = 0.2
    while t < 2.8:
        words.append(_w(t, t + 0.3))
        t += 0.4
    t = 6.0
    while t < 9.6:
        words.append(_w(t, t + 0.3))
        t += 0.4
    t = 10.2
    while t < 19.6:
        words.append(_w(t, t + 0.3))
        t += 0.4
    transcript = Transcript(
        language="en",
        language_probability=1.0,
        duration=20.0,
        segments=[TranscriptSegment(start=0.2, end=19.8, text="x", words=words)],
    )

    asset = probe(synth_source)
    analysis, reel = _make_analysis_and_reel(asset.id)
    analysis = analysis.model_copy(update={"transcript": transcript})
    reel = reel.model_copy(
        update={"scene_indices": [0, 1], "end_sec": 20.0, "duration_sec": 20.0}
    )
    config = ComposeConfig(
        aspect="9:16",
        video_preset="ultrafast",
        no_music=True,
        beat_sync=False,
        jump_cuts="on",
        captions=CaptionStyle(mode="off"),
        effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False, lut=None),
        smart_mode=False,
        transition=TransitionStyle(kind="fade", duration_sec=0.4),
    )

    manifest = await compose(asset, reel, analysis, config)
    mezz = Path(manifest.mezzanine_path)
    assert mezz.exists()
    dur = float(_ffprobe(mezz)["format"]["duration"])
    # Scene 0 splits at the 3.0-6.0 silence: shots (0-3.15)(5.85-10)(10-20)
    # -> 3.15 + 4.15 + 10 - 0.04 (jump cut) - 0.4 (scene fade) = 16.86s.
    assert dur == pytest.approx(16.86, abs=0.35)
