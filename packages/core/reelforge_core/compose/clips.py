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
    # Photo shots carry no source timeline: they occupy mezzanine time but
    # map to no span of the original footage (captions skip them), and their
    # motion is already baked in (the render graph skips Ken Burns).
    is_photo: bool = False
    photo_asset_id: str | None = None
    # Which source the video shot came from (timeline mode can mix assets).
    asset_id: str | None = None
    # Linear gain applied to this shot's own audio in the render graph
    # (editor per-shot volume/mute). 1.0 = untouched.
    volume: float = 1.0
    # Editor-requested Ken Burns on a video shot (timeline mode). Scene-mode
    # clips leave this False and rely on the low-energy auto-trigger.
    force_ken_burns: bool = False
    # Playback speed baked into the clip at extraction (see TimelineShot.speed).
    # duration is already speed-scaled; captions suppress speed != 1 shots.
    speed: float = 1.0
    # Static (or drifting) digital zoom applied in the render graph.
    punch_in: float | None = None
    punch_in_animated: bool = False


def _atempo_chain(speed: float) -> str:
    """ffmpeg atempo accepts 0.5-2.0 per instance; chain factors outside it.
    Supported speed range 0.25-4.0 decomposes into at most two instances."""
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={f:.6g}" for f in factors)


def _video_filter_chain(
    *,
    width: int,
    height: int,
    fps: int,
    is_hdr: bool,
    pan: tuple[float, float] | None = None,
    pan_window: tuple[float, float] | None = None,
    speed: float = 1.0,
) -> str:
    """Scale chain for one clip.

    `pan=(x0_frac, x1_frac)` switches from letterbox to a subject-tracking
    crop: a target-aspect window cut from the source, its x-center drifting
    linearly from x0 to x1 over `pan_window=(in_ts, duration)` (source-time
    coordinates — the clip command output-seeks, so filter `t` is source
    time; the expression clamps p into [0,1] so either seek semantic is safe).
    """
    parts: list[str] = []
    if speed != 1.0:
        # Retiming FIRST — the fps filter later in the chain then resamples
        # to constant frame rate (dup/drop), so downstream xfade math holds.
        parts.append(f"setpts=PTS/{speed:.6g}")
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
    speed: float = 1.0,
) -> list[str]:
    w, h = config.resolution
    # -ss/-to are OUTPUT options here (accurate-seek re-encode): they act on
    # post-filter timestamps. setpts=PTS/speed retimes the stream, so the
    # seek window (and the pan expression's time base) scales by 1/speed.
    ss = in_ts / speed
    to = out_ts / speed
    vf = _video_filter_chain(
        width=w,
        height=h,
        fps=config.target_fps,
        is_hdr=is_hdr,
        pan=pan,
        pan_window=(ss, max(0.001, to - ss)),
        speed=speed,
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
        f"{ss:.3f}",
        "-to",
        f"{to:.3f}",
        "-vf",
        vf,
    ]
    if has_audio:
        af = (
            "aresample=async=1000:first_pts=0,"
            "aformat=sample_rates=48000:channel_layouts=stereo"
        )
        if speed != 1.0:
            # Keep the (muted-in-graph) audio stream duration-matched to the
            # retimed video so the acrossfade chain stays consistent.
            af += "," + _atempo_chain(speed)
        args += [
            "-af",
            af,
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
    reel_start: float | None = None,
    reel_end: float | None = None,
) -> tuple[float, float]:
    """Pure: the source in/out timestamps for the clip at `position`.

    Selection v2 reel bounds are authoritative and may fall mid-scene: the
    outer clips are clamped to `reel_start`/`reel_end` FIRST (scene-aligned
    reels clamp to their own scene edges — a no-op), then user trim offsets
    apply, then the outer bounds snap off the middle of any spoken word
    (speech-safe cuts). Caption timing mirrors this computation — keep them
    in sync.
    """
    from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start

    in_ts = scene.start_sec
    out_ts = scene.end_sec
    if position == 0:
        if reel_start is not None:
            in_ts = max(in_ts, reel_start)
        in_ts = max(0.0, in_ts + config.trim_start_offset_sec)
    if position == n_scenes - 1:
        if reel_end is not None:
            out_ts = min(out_ts, reel_end)
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
    shot_bounds: "list | None" = None,
) -> list[ClipInfo]:
    """Extract one normalized clip per scene in `reel.scene_indices`.

    `end_trims` (len n-1, from beat sync) shortens interior clips so
    transitions land on music beats; never applied to the last clip.
    `shot_bounds` (a list of styles.PlannedShot — jump cuts, style plans)
    replaces the scene list with FINAL bounds + per-shot speed/punch-in/Ken
    Burns: clip_bounds is skipped since the pipeline already applied
    clamps/trims/snapping when planning.
    """
    from reelforge_core.compose.reframe import estimate_pan, should_crop

    clips_dir = reel_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    plan = list(shot_bounds) if shot_bounds is not None else None
    is_hdr = (asset.probe.color_transfer or "") in HDR_TRANSFERS
    total = len(plan) if plan is not None else len(reel.scene_indices)
    sem: asyncio.Semaphore = asyncio.Semaphore(min(4, max(1, total)))
    done = 0

    source_mtime = int(asset.path.stat().st_mtime)
    w, h = config.resolution
    n_scenes = total
    crop_track = should_crop(
        asset.probe.width or 0, asset.probe.height or 0, w, h, config.effects.reframe
    )

    async def _one(position: int, scene_idx: int, planned) -> ClipInfo:
        scene = analysis.scenes[scene_idx]
        speed = 1.0
        punch_in = None
        punch_in_animated = False
        force_kb = False
        if planned is not None:
            # Final bounds + per-shot treatment from the style planner.
            in_ts, out_ts = planned.in_ts, planned.out_ts
            speed = planned.speed
            punch_in = planned.punch_in
            punch_in_animated = planned.punch_in_animated
            force_kb = planned.force_ken_burns
        else:
            # Reel-bound clamp + trim offsets + speech-safe snapping (outer clips).
            in_ts, out_ts = clip_bounds(
                position,
                n_scenes,
                scene,
                config,
                analysis,
                reel_start=reel.start_sec,
                reel_end=reel.end_sec,
            )
        if end_trims is not None and position < n_scenes - 1 and position < len(end_trims):
            # Beat trims are mezzanine seconds -> scale into source seconds.
            out_ts = max(in_ts + 0.5 * speed, out_ts - end_trims[position] * speed)
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
                "speed": f"{speed:.4f}",
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
                speed=speed,
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
            duration=(out_ts - in_ts) / speed,
            has_audio=asset.has_audio,
            effects_applied=[],
            speed=speed,
            punch_in=punch_in,
            punch_in_animated=punch_in_animated,
            force_ken_burns=force_kb,
        )

    results: list[ClipInfo | None] = [None] * total
    if plan is not None:
        tasks = [
            asyncio.create_task(_one(i, ps.scene_index, ps))
            for i, ps in enumerate(plan)
        ]
    else:
        tasks = [
            asyncio.create_task(_one(i, scene_idx, None))
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



# ---------------------------------------------------------------------------
# Timeline mode: arbitrary shots from any project asset
# ---------------------------------------------------------------------------


async def extract_timeline_clips(
    shots: list,
    sources: dict[str, MediaAsset],
    analyses: dict[str, "AnalysisReport | None"],
    config: ComposeConfig,
    reel_dir: Path,
    log_file: Path,
    progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    end_trims: list[float] | None = None,
) -> list[ClipInfo]:
    """Render an edited timeline's shots into normalized clips, in order.

    Video shots are arbitrary [in, out] ranges of any source (not limited to
    detected scenes); photos come from `compose.photos`. Speech-safe snapping
    applies to the reel's outer bounds when the source has a transcript;
    beat-sync `end_trims` shorten interior shots exactly as in scene mode.
    """
    from reelforge_core.compose.photos import render_photo_clip
    from reelforge_core.compose.reframe import estimate_pan, should_crop
    from reelforge_core.compose.speech_snap import flatten_words, snap_end, snap_start

    clips_dir = reel_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    w, h = config.resolution
    n = len(shots)
    total = n
    done = 0
    sem: asyncio.Semaphore = asyncio.Semaphore(4)

    async def _video(position: int, shot) -> ClipInfo:
        asset = sources[shot.asset_id]
        analysis = analyses.get(shot.asset_id)
        src_dur = asset.probe.duration_s or float("inf")
        in_ts = max(0.0, min(shot.in_ts, src_dur))
        out_ts = max(in_ts + 0.1, min(shot.out_ts, src_dur))
        # Outer-bound speech snapping, same rule as scene mode.
        if config.speech_safe_cuts and analysis is not None and analysis.transcript:
            words = flatten_words(analysis.transcript)
            nudge = config.speech_safe_max_nudge_sec
            if position == 0:
                in_ts = max(0.0, snap_start(in_ts, words, nudge))
            if position == n - 1:
                out_ts = max(in_ts + 0.1, snap_end(out_ts, words, nudge))
        speed = float(getattr(shot, "speed", 1.0) or 1.0)
        if end_trims is not None and position < n - 1 and position < len(end_trims):
            # Beat trims are MEZZANINE seconds; a sped shot must lose
            # speed-times as many source seconds to shrink by the same amount.
            out_ts = max(in_ts + 0.5, out_ts - end_trims[position] * speed)

        is_hdr = (asset.probe.color_transfer or "") in HDR_TRANSFERS
        crop_track = should_crop(
            asset.probe.width or 0, asset.probe.height or 0, w, h, config.effects.reframe
        )
        pan = None
        if crop_track:
            pan = await asyncio.to_thread(estimate_pan, asset.path, in_ts, out_ts)
        out_path = clips_dir / f"clip_{position:04d}.mp4"
        source_mtime = int(asset.path.stat().st_mtime)
        cache_key = file_cache.compute_key(
            "clip",
            {
                "asset_id": asset.id,
                "scene_idx": -1,
                "source_mtime": source_mtime,
                "speed": f"{speed:.4f}",
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
                speed=speed,
            )
            async with sem:
                await asyncio.to_thread(run_ffmpeg, cmd, log_file=log_file)
            try:
                cache_target = file_cache.path_for("clip", cache_key, "mp4")
                import shutil as _shutil

                _shutil.copy2(out_path, cache_target)
                file_cache.register(cache_key, "clip", cache_target)
                file_cache.evict_if_over_cap("clip", file_cache.cap_from_env("clip", 20.0))
            except Exception:  # pragma: no cover
                log.exception("clip cache write failed for %s", cache_key)
        return ClipInfo(
            path=out_path,
            scene_index=-1,
            in_ts=in_ts,
            out_ts=out_ts,
            duration=(out_ts - in_ts) / speed,
            has_audio=asset.has_audio,
            effects_applied=[],
            asset_id=asset.id,
            volume=shot.effective_gain,
            force_ken_burns=bool(getattr(shot, "ken_burns", False)),
            speed=speed,
            punch_in=getattr(shot, "punch_in", None),
            punch_in_animated=bool(getattr(shot, "punch_in_animated", False)),
        )

    async def _photo(position: int, shot) -> ClipInfo:
        from reelforge_core.models import PhotoInsert

        out_path = clips_dir / f"clip_{position:04d}.mp4"
        insert = PhotoInsert(
            asset_id=shot.asset_id,
            path=shot.path,
            position=position,
            duration_sec=shot.duration_sec,
            ken_burns=shot.ken_burns,
        )
        await render_photo_clip(insert, out_path, config, log_file, pan_index=position)
        return ClipInfo(
            path=out_path,
            scene_index=-1,
            in_ts=0.0,
            out_ts=shot.duration_sec,
            duration=max(0.2, shot.duration_sec),
            has_audio=True,
            effects_applied=["ken_burns"] if shot.ken_burns else [],
            is_photo=True,
            photo_asset_id=shot.asset_id,
            asset_id=shot.asset_id,
        )

    async def _one(position: int, shot) -> ClipInfo:
        if shot.kind == "photo":
            return await _photo(position, shot)
        return await _video(position, shot)

    results: list[ClipInfo | None] = [None] * total
    tasks = [asyncio.create_task(_one(i, shot)) for i, shot in enumerate(shots)]
    for coro in asyncio.as_completed(tasks):
        info = await coro
        position = int(info.path.stem.split("_")[-1])
        results[position] = info
        done += 1
        if progress_cb is not None:
            await progress_cb(done, total)
    return [r for r in results if r is not None]
