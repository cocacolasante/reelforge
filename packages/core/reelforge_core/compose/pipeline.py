"""Composition orchestrator: clips → captions → music → final render → mezzanine.mp4."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from reelforge_core.compose.captions import build_captions
from reelforge_core.compose.clips import extract_clips
from reelforge_core.compose.graph import ffmpeg_version, run_ffmpeg
from reelforge_core.compose.graph_builder import build_final_command
from reelforge_core.compose.music import (
    load_music_library,
    prepare_music,
    select_track,
)
from reelforge_core.errors import ComposeError, FFmpegError
from reelforge_core.ingest import MediaAsset
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    REELFORGE_VERSION,
    AnalysisReport,
    ComposeConfig,
    ComposeManifest,
    ProgressEvent,
    RankedReel,
)

log = logging.getLogger(__name__)

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]

STAGE_WEIGHTS: dict[str, float] = {
    "prepare": 0.02,
    "clips": 0.35,
    "captions": 0.03,
    "music": 0.05,
    "render": 0.53,
    "finalize": 0.02,
}

FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):([\d.]+)")


async def _noop(_: ProgressEvent) -> None:
    return None


def _overall(stage: str, stage_progress: float) -> float:
    total = 0.0
    for s, w in STAGE_WEIGHTS.items():
        if s == stage:
            total += w * max(0.0, min(1.0, stage_progress))
            break
        total += w
    return min(1.0, total)


def _emit(stage: str, sp: float, message: str | None = None) -> ProgressEvent:
    return ProgressEvent(stage=stage, stage_progress=sp, overall_progress=_overall(stage, sp), message=message)  # type: ignore[arg-type]


def resolve_lut(lut_id: str) -> Path | None:
    """Resolve a LUT id to a file under /app/assets/luts or /data/luts.

    Supports either "name" (looked up as `{name}.cube`) or "path/to/file.cube".
    """
    if "/" in lut_id or lut_id.endswith(".cube"):
        p = Path(lut_id)
        if p.is_absolute():
            return p if p.exists() else None
    stem = lut_id if lut_id.endswith(".cube") else f"{lut_id}.cube"
    for base in (Path("/data/luts"), Path("/app/assets/luts")):
        candidate = base / stem
        if candidate.exists():
            return candidate
    return None


async def _run_ffmpeg_with_progress(
    args: list[str],
    *,
    log_file: Path,
    total_duration_sec: float,
    progress: ProgressCallback,
) -> None:
    """Run FFmpeg and emit render-stage progress from stderr `time=` lines."""
    # Append to the command log so it's identical to everything else that goes
    # through run_ffmpeg.
    from shlex import quote

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(" ".join(quote(a) for a in args) + "\n\n")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_buf: list[str] = []

    async def _reader() -> None:
        assert proc.stderr is not None
        last_emit = 0.0
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            stderr_buf.append(text)
            m = FFMPEG_TIME_RE.search(text)
            if m:
                h = int(m.group(1))
                mi = int(m.group(2))
                s = float(m.group(3))
                t = h * 3600 + mi * 60 + s
                sp = max(0.0, min(1.0, t / total_duration_sec if total_duration_sec > 0 else 1.0))
                now = time.monotonic()
                if now - last_emit >= 0.5 or sp >= 1.0:
                    last_emit = now
                    await progress(_emit("render", sp))
            # Cap buffered stderr so long runs don't bloat memory.
            if len(stderr_buf) > 4000:
                del stderr_buf[:2000]

    reader_task = asyncio.create_task(_reader())
    try:
        rc = await proc.wait()
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

    if rc != 0:
        tail = "".join(stderr_buf)[-4000:]
        raise FFmpegError(
            f"ffmpeg (mezzanine render) exited {rc}",
            stderr=tail,
            cmdline=" ".join(args),
        )


async def compose(
    asset: MediaAsset,
    reel: RankedReel,
    analysis: AnalysisReport,
    config: ComposeConfig,
    progress: ProgressCallback = _noop,
) -> ComposeManifest:
    t_start = time.monotonic()
    data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    working = data_dir / "working" / asset.id
    reel_dir = working / "reels" / reel.candidate_id
    reel_dir.mkdir(parents=True, exist_ok=True)
    (reel_dir / "tmp").mkdir(parents=True, exist_ok=True)
    log_file = reel_dir / "ffmpeg_commands.log"

    # Smart-mode: resolve "auto" transition kind + "auto" LUT before any work.
    # The resolved config is what gets persisted in compose.json so the UI can
    # show "the AI picked: slideleft + warm LUT + uplift-acoustic".
    from reelforge_core.compose.auto import resolve_smart_config

    config = resolve_smart_config(config, reel, analysis)

    # ----- prepare -----
    await progress(_emit("prepare", 0.0))
    library = load_music_library()
    track = select_track(library, config, reel)

    # Beat sync: analyze the chosen track (tempo + phase) and compute the
    # per-clip end trims that put each crossfade midpoint on a beat. Must
    # happen before clip extraction — the trims change clip bounds.
    end_trims: list[float] | None = None
    beat_grid = None
    if (
        track is not None
        and config.beat_sync
        and len(reel.scene_indices) > 1
        and config.transition.kind != "cut"
    ):
        from reelforge_core.compose.beats import compute_beat_end_trims, detect_beats
        from reelforge_core.compose.clips import clip_bounds

        beat_grid = await asyncio.to_thread(detect_beats, Path(track.path))
        if beat_grid is not None:
            n = len(reel.scene_indices)
            planned = [
                clip_bounds(pos, n, analysis.scenes[idx], config, analysis)
                for pos, idx in enumerate(reel.scene_indices)
            ]
            durations = [out - in_ for in_, out in planned]
            end_trims = compute_beat_end_trims(
                durations,
                config.transition.duration_sec,
                beat_grid,
                config.beat_sync_max_adjust_sec,
            )
            if any(t > 0 for t in end_trims):
                log.info(
                    "beat sync: %.1f BPM (phase %.2fs), trims %s",
                    beat_grid.bpm,
                    beat_grid.phase_sec,
                    end_trims,
                )
    await progress(_emit("prepare", 1.0))

    # ----- clips -----
    async def _clip_progress(done: int, total: int) -> None:
        sp = done / total if total else 1.0
        await progress(_emit("clips", sp))

    await progress(_emit("clips", 0.0))
    try:
        clips = await extract_clips(
            asset,
            reel,
            analysis,
            config,
            reel_dir,
            log_file,
            _clip_progress,
            end_trims=end_trims,
        )
    except FFmpegError as exc:
        raise ComposeError(f"clip extraction failed: {exc}") from exc
    await progress(_emit("clips", 1.0))

    # ----- captions -----
    await progress(_emit("captions", 0.0))
    try:
        captions_path = build_captions(
            reel, analysis, config, reel_dir, end_trims=end_trims
        )
    except Exception as exc:
        raise ComposeError(f"caption build failed: {exc}") from exc
    captions_for_render: Path | None
    if config.captions.mode == "off" or analysis.transcript is None:
        captions_for_render = None
    else:
        captions_for_render = captions_path
    await progress(_emit("captions", 1.0))

    # ----- music -----
    await progress(_emit("music", 0.0))
    music_path: Path | None = None
    xfade_dur = (
        config.transition.duration_sec if config.transition.kind != "cut" else 0.04
    )
    target_duration = max(
        0.1,
        sum(c.duration for c in clips) - max(0, len(clips) - 1) * xfade_dur,
    )
    if track is not None:
        try:
            music_path = prepare_music(track, target_duration, config, reel_dir, log_file)
        except FFmpegError as exc:
            raise ComposeError(f"music prep failed: {exc}") from exc
    await progress(_emit("music", 1.0))

    # ----- render -----
    await progress(_emit("render", 0.0))
    mezzanine_path = reel_dir / "mezzanine.mp4"
    plan = build_final_command(
        clips=clips,
        analysis=analysis,
        music_path=music_path,
        captions_path=captions_for_render,
        config=config,
        output_path=mezzanine_path,
    )

    try:
        await _run_ffmpeg_with_progress(
            plan.args,
            log_file=log_file,
            total_duration_sec=plan.mezzanine_duration_sec,
            progress=progress,
        )
    except FFmpegError as exc:
        raise ComposeError(f"mezzanine render failed: {exc}") from exc
    await progress(_emit("render", 1.0))

    # ----- finalize -----
    await progress(_emit("finalize", 0.0))
    scene_clip_map: list[dict] = []
    for c in clips:
        scene_clip_map.append(
            {
                "scene_index": c.scene_index,
                "clip_path": str(c.path),
                "in_ts": c.in_ts,
                "out_ts": c.out_ts,
                "effects_applied": c.effects_applied,
            }
        )

    manifest = ComposeManifest(
        asset_id=asset.id,
        reel_id=reel.candidate_id,
        reel_title=reel.title,
        reel_hook=reel.hook,
        config=config,
        chosen_music=track,
        mezzanine_path=str(mezzanine_path),
        duration_sec=round(plan.mezzanine_duration_sec, 3),
        width=config.resolution[0],
        height=config.resolution[1],
        fps=float(config.target_fps),
        scene_clip_map=scene_clip_map,
        ffmpeg_version=ffmpeg_version(),
        reelforge_version=REELFORGE_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=round(time.monotonic() - t_start, 3),
    )
    write_json_atomic(reel_dir / "compose.json", json.loads(manifest.model_dump_json()))

    # Cleanup tmp on success (keep clips + logs for debugging / Phase 4 export).
    shutil.rmtree(reel_dir / "tmp", ignore_errors=True)

    await progress(_emit("finalize", 1.0))
    return manifest
