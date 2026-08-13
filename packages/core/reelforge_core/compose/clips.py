"""Extract normalized per-scene clips. Accurate-seek re-encode (slow but correct)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from reelforge_core import cache as file_cache
from reelforge_core.compose.graph import run_ffmpeg
from reelforge_core.ingest import MediaAsset
from reelforge_core.models import AnalysisReport, ComposeConfig, RankedReel

log = logging.getLogger(__name__)

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass
class ClipInfo:
    path: Path
    scene_index: int
    in_ts: float
    out_ts: float
    duration: float
    has_audio: bool
    effects_applied: list[str]


def _video_filter_chain(
    *,
    width: int,
    height: int,
    fps: int,
    is_hdr: bool,
    pan: tuple[float, float] | None = None,
    pan_window: tuple[float, float] | None = None,
) -> str:
    """Scale chain for one clip.

    `pan=(x0_frac, x1_frac)` switches from letterbox to a subject-tracking
    crop: a target-aspect window cut from the source, its x-center drifting
    linearly from x0 to x1 over `pan_window=(in_ts, duration)` (source-time
    coordinates — the clip command output-seeks, so filter `t` is source
    time; the expression clamps p into [0,1] so either seek semantic is safe).
    """
    parts: list[str] = []
    if is_hdr:
        # HDR → SDR tonemap. Verbose but correct.
        parts.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
        )
    if pan is not None:
        in_ts, duration = pan_window or (0.0, 1.0)
        x0, x1 = pan
        # Crop window: full source height, width matching the target aspect.
        crop_w = f"floor(ih*{width}/{height}/2)*2"
        progress = f"min(max((t-{in_ts:.3f})/{max(duration, 0.001):.3f}\\,0)\\,1)"
        center = f"({x0:.4f}+({x1 - x0:.4f})*{progress})*iw"
        x_expr = f"min(max({center}-({crop_w})/2\\,0)\\,iw-({crop_w}))"
        parts.append(
            f"crop=w={crop_w}:h=ih:x='{x_expr}':y=0,"
            f"scale={width}:{height},setsar=1,fps={fps}"
        )
    else:
        parts.append(
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1,"
            f"fps={fps}"
        )
    return ",".join(parts)


def build_clip_command(
    *,
    source: Path,
    out_path: Path,
    in_ts: float,
    out_ts: float,
    config: ComposeConfig,
    has_audio: bool,
    is_hdr: bool,
    pan: tuple[float, float] | None = None,
) -> list[str]:
    w, h = config.resolution
    vf = _video_filter_chain(
        width=w,
        height=h,
        fps=config.target_fps,
        is_hdr=is_hdr,
        pan=pan,
        pan_window=(in_ts, max(0.001, out_ts - in_ts)),
    )
    args: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ss",
        f"{in_ts:.3f}",
        "-to",
        f"{out_ts:.3f}",
        "-vf",
        vf,
    ]
    if has_audio:
        args += [
            "-af",
            "aresample=async=1000:first_pts=0,"
            "aformat=sample_rates=48000:channel_layouts=stereo",
            "-c:a",
            "aac",
            "-b:a",
            f"{config.audio_bitrate_kbps}k",
        ]
    else:
        args += ["-an"]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        config.clip_preset,  # intermediate; mezzanine uses effective_mezz_preset
        "-crf",
        str(config.clip_crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        # Strip wall-clock metadata so intermediates are deterministic.
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(out_path),
    ]
    return args


def clip_bounds(
    position: int,
    n_scenes: int,
    scene,
    config: ComposeConfig,
    analysis: AnalysisReport,
) -> tuple[float, float]:
    """Pure: the source in/out timestamps for the clip at `position`.

    Applies user trim offsets to the reel's outer clips, then snaps the outer
    bounds off the middle of any spoken word (speech-safe cuts). Caption
    timing mirrors this computation — keep them in sync.
    """
    from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start

    in_ts = scene.start_sec
    out_ts = scene.end_sec
    if position == 0:
        in_ts = max(0.0, in_ts + config.trim_start_offset_sec)
    if position == n_scenes - 1:
        out_ts = max(in_ts + 0.1, out_ts - config.trim_end_offset_sec)
    if config.speech_safe_cuts and analysis.transcript is not None:
        words = flatten_words(analysis.transcript)
        nudge = config.speech_safe_max_nudge_sec
        if position == 0:
            in_ts = max(0.0, snap_start(in_ts, words, nudge))
        if position == n_scenes - 1:
            out_ts = max(in_ts + 0.1, snap_end(out_ts, words, nudge))
    return in_ts, out_ts


async def extract_clips(
    asset: MediaAsset,
    reel: RankedReel,
    analysis: AnalysisReport,
    config: ComposeConfig,
    reel_dir: Path,
    log_file: Path,
    progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    end_trims: list[float] | None = None,
) -> list[ClipInfo]:
    """Extract one normalized clip per scene in `reel.scene_indices`.

    `end_trims` (len n-1, from beat sync) shortens interior clips so
    transitions land on music beats; never applied to the last clip.
    """
    from reelforge_core.compose.reframe import estimate_pan, should_crop

    clips_dir = reel_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    is_hdr = (asset.probe.color_transfer or "") in HDR_TRANSFERS
    sem: asyncio.Semaphore = asyncio.Semaphore(min(4, max(1, len(reel.scene_indices))))
    total = len(reel.scene_indices)
    done = 0

    source_mtime = int(asset.path.stat().st_mtime)
    w, h = config.resolution
    n_scenes = len(reel.scene_indices)
    crop_track = should_crop(
        asset.probe.width or 0, asset.probe.height or 0, w, h, config.effects.reframe
    )

    async def _one(position: int, scene_idx: int) -> ClipInfo:
        scene = analysis.scenes[scene_idx]
        # Trim offsets + speech-safe snapping for the reel's outer clips.
        in_ts, out_ts = clip_bounds(position, n_scenes, scene, config, analysis)
        if end_trims is not None and position < n_scenes - 1 and position < len(end_trims):
            out_ts = max(in_ts + 0.5, out_ts - end_trims[position])
        pan: tuple[float, float] | None = None
        if crop_track:
            pan = await asyncio.to_thread(estimate_pan, asset.path, in_ts, out_ts)
        out_path = clips_dir / f"clip_{position:04d}.mp4"

        # Clip cache: same asset + scene + source mtime + aspect/fps/resolution
        # produces byte-identical output (ffmpeg is deterministic with +bitexact).
        cache_key = file_cache.compute_key(
            "clip",
            {
                "asset_id": asset.id,
                "scene_idx": scene_idx,
                "source_mtime": source_mtime,
                "in_ts": f"{in_ts:.3f}",
                "out_ts": f"{out_ts:.3f}",
                "width": w,
                "height": h,
                "fps": config.target_fps,
                "has_audio": int(asset.has_audio),
                "hdr": int(is_hdr),
                "crf": config.clip_crf,
                "preset": config.clip_preset,
                "pan": "none" if pan is None else f"{pan[0]:.4f}-{pan[1]:.4f}",
            },
        )
        cached = file_cache.lookup(cache_key)
        if cached is not None:
            # Copy/link the cached clip into the reel's clips dir so downstream
            # logic (scene_clip_map, FFmpeg chain) sees a stable path.
            try:
                if out_path.exists():
                    out_path.unlink()
                os.link(cached, out_path)
            except OSError:
                import shutil as _shutil
                _shutil.copy2(cached, out_path)
        else:
            cmd = build_clip_command(
                source=asset.path,
                out_path=out_path,
                in_ts=in_ts,
                out_ts=out_ts,
                config=config,
                has_audio=asset.has_audio,
                is_hdr=is_hdr,
                pan=pan,
            )
            async with sem:
                await asyncio.to_thread(run_ffmpeg, cmd, log_file=log_file)
            # Copy into the cache directory and register.
            try:
                cache_target = file_cache.path_for("clip", cache_key, "mp4")
                import shutil as _shutil
                _shutil.copy2(out_path, cache_target)
                file_cache.register(cache_key, "clip", cache_target)
                # Opportunistic LRU eviction
                file_cache.evict_if_over_cap(
                    "clip", file_cache.cap_from_env("clip", 20.0)
                )
            except Exception:  # pragma: no cover
                log.exception("clip cache write failed for %s", cache_key)
        return ClipInfo(
            path=out_path,
            scene_index=scene_idx,
            in_ts=in_ts,
            out_ts=out_ts,
            duration=out_ts - in_ts,
            has_audio=asset.has_audio,
            effects_applied=[],
        )

    results: list[ClipInfo | None] = [None] * total
    tasks = [
        asyncio.create_task(_one(i, scene_idx))
        for i, scene_idx in enumerate(reel.scene_indices)
    ]
    for coro in asyncio.as_completed(tasks):
        info = await coro
        # Place by position (clip_NNNN.mp4 encodes position)
        position = int(info.path.stem.split("_")[-1])
        results[position] = info
        done += 1
        if progress_cb is not None:
            await progress_cb(done, total)

    out: list[ClipInfo] = []
    for r in results:
        assert r is not None
        out.append(r)
    return out
