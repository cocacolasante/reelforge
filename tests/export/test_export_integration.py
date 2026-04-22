"""End-to-end export integration. Runs real FFmpeg + ffprobe on a fresh mezzanine."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reelforge_core import probe
from reelforge_core.compose import compose
from reelforge_core.errors import (
    MezzanineNotFoundError,
    OutputVerificationError,
    PresetNotFoundError,
)
from reelforge_core.export import PRESET_SPEC_VERSION, PRESETS, export
from reelforge_core.export.presets import PRORES_FOURCC
from reelforge_core.export.verify import verify_export
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    ExportManifest,
    RankedReel,
    REELFORGE_VERSION,
    ReelScores,
    Scene,
    SceneSemantics,
    TransitionStyle,
)


def _build_source(tmp: Path) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    parts = []
    for i, c in enumerate(["red", "green", "blue"]):
        p = tmp / f"p{i}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c={c}:s=640x360:d=10:r=25",
                "-f", "lavfi", "-i", f"sine=frequency={220 + i * 80}:duration=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
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
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", str(src),
        ],
        check=True,
        capture_output=True,
    )
    return src


def _make_analysis_reel(asset_id: str):
    scenes = [
        Scene(
            index=i,
            start_sec=i * 10.0,
            end_sec=(i + 1) * 10.0,
            start_frame=i * 250,
            end_frame=(i + 1) * 250,
            thumbnail_path=f"thumbs/scene_{i:04d}.jpg",
        )
        for i in range(3)
    ]
    sems = [
        SceneSemantics(
            scene_index=i,
            summary="s",
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
        candidate_id="exportcand00000",
        scene_indices=[0, 1, 2],
        start_sec=0.0,
        end_sec=30.0,
        duration_sec=30.0,
        title="Export test",
        hook="hook",
        justification="j",
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


async def _mezzanine_for_tests(src: Path, isolated_data_dir: Path):
    asset = probe(src)
    analysis, reel = _make_analysis_reel(asset.id)
    config = ComposeConfig(
        aspect="9:16",
        target_fps=30,
        video_preset="ultrafast",
        captions=CaptionStyle(mode="off"),
        transition=TransitionStyle(kind="fade", duration_sec=0.4),
        effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False),
        no_music=True,  # keep test fast + no music fixture needed
    )
    manifest = await compose(asset, reel, analysis, config)
    return asset.id, reel.candidate_id, Path(manifest.mezzanine_path)


@pytest.fixture(scope="function")
async def mezzanine_ready(
    tmp_path_factory: pytest.TempPathFactory, isolated_data_dir: Path
):
    src = _build_source(tmp_path_factory.mktemp("src"))
    return await _mezzanine_for_tests(src, isolated_data_dir)


@pytest.mark.asyncio
async def test_export_h264_social(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    manifest = await export(asset_id, reel_id, "mp4_h264_social")
    assert manifest.video_codec == "h264"
    assert manifest.video_pixel_format == "yuv420p"
    assert manifest.audio_codec == "aac"
    assert manifest.output_path.endswith(".mp4")
    assert manifest.preset_spec_version == PRESET_SPEC_VERSION
    out = Path(manifest.output_path)
    assert out.exists() and out.stat().st_size > 0
    # Sidecar must exist and parse.
    sidecar = out.with_name(out.stem + ".export.json")
    assert sidecar.exists()
    loaded = ExportManifest.model_validate_json(sidecar.read_text())
    assert loaded.input_mezzanine_sha256 == manifest.input_mezzanine_sha256


@pytest.mark.asyncio
async def test_export_h265_hq_has_hvc1_tag(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    manifest = await export(asset_id, reel_id, "mp4_h265_hq")
    assert manifest.video_codec == "hevc"
    # Verify tag via direct ffprobe readout too
    out = Path(manifest.output_path)
    data = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert v["codec_tag_string"] == "hvc1"


@pytest.mark.asyncio
async def test_export_prores_422_has_apcn_fourcc(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    manifest = await export(asset_id, reel_id, "mov_prores_422")
    assert manifest.video_codec == "prores"
    assert manifest.video_pixel_format == "yuv422p10le"
    out = Path(manifest.output_path)
    data = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert v["codec_tag_string"] == PRORES_FOURCC["2"]  # apcn


@pytest.mark.asyncio
async def test_export_prores_hq_has_apch_fourcc(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    manifest = await export(asset_id, reel_id, "mov_prores_hq")
    assert manifest.video_codec == "prores"
    out = Path(manifest.output_path)
    data = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert v["codec_tag_string"] == PRORES_FOURCC["3"]  # apch


@pytest.mark.asyncio
async def test_skip_if_exists_returns_same_manifest(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    first = await export(asset_id, reel_id, "mp4_h264_social")
    out_mtime_1 = Path(first.output_path).stat().st_mtime
    # Second call should not re-transcode — output mtime unchanged.
    second = await export(asset_id, reel_id, "mp4_h264_social")
    out_mtime_2 = Path(second.output_path).stat().st_mtime
    assert out_mtime_1 == out_mtime_2
    assert second.input_mezzanine_sha256 == first.input_mezzanine_sha256


@pytest.mark.asyncio
async def test_force_retranscodes(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    first = await export(asset_id, reel_id, "mp4_h264_social")
    out_mtime_1 = Path(first.output_path).stat().st_mtime
    # Bump mtime to -1s so we can tell the difference reliably.
    import os, time

    os.utime(Path(first.output_path), (out_mtime_1 - 2, out_mtime_1 - 2))
    time.sleep(0.01)
    second = await export(asset_id, reel_id, "mp4_h264_social", force=True)
    out_mtime_2 = Path(second.output_path).stat().st_mtime
    assert out_mtime_2 > out_mtime_1 - 2


@pytest.mark.asyncio
async def test_stale_after_recompose(
    tmp_path_factory: pytest.TempPathFactory, isolated_data_dir: Path
) -> None:
    # First mezzanine
    src = _build_source(tmp_path_factory.mktemp("src"))
    asset_id, reel_id, mezz = await _mezzanine_for_tests(src, isolated_data_dir)
    first = await export(asset_id, reel_id, "mp4_h264_social")
    first_hash = first.input_mezzanine_sha256
    # Flip a byte in the mezzanine → changes sha256 → skip-if-exists
    # should detect staleness and re-transcode.
    b = bytearray(mezz.read_bytes())
    # Skip the first 512 bytes so we don't corrupt the moov atom at head.
    b[1024] ^= 0x01
    mezz.write_bytes(bytes(b))
    second = await export(asset_id, reel_id, "mp4_h264_social")
    assert second.input_mezzanine_sha256 != first_hash


@pytest.mark.asyncio
async def test_missing_mezzanine_raises(isolated_data_dir: Path) -> None:
    with pytest.raises(MezzanineNotFoundError):
        await export("a" * 64, "ghostcandidate00", "mp4_h264_social")


@pytest.mark.asyncio
async def test_unknown_preset_raises(mezzanine_ready) -> None:
    asset_id, reel_id, _ = mezzanine_ready
    with pytest.raises(PresetNotFoundError):
        await export(asset_id, reel_id, "nope_not_real")


@pytest.mark.asyncio
async def test_verify_rejects_wrong_codec_claim(mezzanine_ready) -> None:
    """Feed a real h264 file while claiming it's h265 → verify raises."""
    asset_id, reel_id, mezz = mezzanine_ready
    # The mezzanine itself is h264; verifying it against the h265 preset must raise.
    with pytest.raises(OutputVerificationError):
        verify_export(mezz, PRESETS["mp4_h265_hq"])
