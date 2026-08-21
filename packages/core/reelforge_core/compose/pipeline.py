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

from reelforge_core.compose.captions import build_captions, has_dialogue
from reelforge_core.compose.clips import ClipInfo, extract_clips
from reelforge_core.compose.graph import ffmpeg_version, run_ffmpeg
from reelforge_core.compose.graph_builder import build_final_command, resolve_transitions
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


def _load_analysis(data_dir: Path, asset_id: str) -> AnalysisReport | None:
    """analysis.json for a timeline source, or None if it was never analyzed
    (captions/speech-snapping simply don't apply to that shot)."""
    path = data_dir / "working" / asset_id / "analysis.json"
    if not path.exists():
        return None
    try:
        return AnalysisReport.model_validate_json(path.read_text())
    except Exception:
        log.warning("unreadable analysis.json for %s; ignoring", asset_id)
        return None


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

    # ----- shot plan -----
    # Timeline mode (post-generation editing): the config carries the complete
    # shot list — arbitrary ranges of any project video, photos, per-cut
    # transitions and text overlays — and replaces scene-derived shots, trim
    # offsets and photo_inserts entirely.
    timeline = config.timeline if (config.timeline and config.timeline.shots) else None
    sources: dict[str, MediaAsset] = {asset.id: asset}
    analyses: dict[str, AnalysisReport | None] = {analysis.asset_id: analysis}
    per_cut: list[tuple[str, float] | None] | None = None
    planned_durations: list[float]
    if timeline is not None:
        from reelforge_core.ingest import probe as _probe

        for shot in timeline.shots:
            if shot.asset_id in sources:
                continue
            path = Path(shot.path) if shot.path else None
            if path is None or not path.exists():
                raise ComposeError(
                    f"timeline references asset {shot.asset_id} with missing file {shot.path!r}"
                )
            sources[shot.asset_id] = await asyncio.to_thread(_probe, path)
            if shot.kind == "video":
                analyses[shot.asset_id] = _load_analysis(data_dir, shot.asset_id)
        per_cut = [
            (
                (s.transition_after.kind, s.transition_after.duration_sec)
                if s.transition_after is not None
                else None
            )
            for s in timeline.shots[:-1]
        ]
        planned_durations = [s.duration for s in timeline.shots]
    else:
        from reelforge_core.compose.clips import clip_bounds

        n = len(reel.scene_indices)
        planned_durations = [
            (lambda b: b[1] - b[0])(
                clip_bounds(pos, n, analysis.scenes[idx], config, analysis)
            )
            for pos, idx in enumerate(reel.scene_indices)
        ]
    # Per-cut (xfade name, duration) for the planned shots. Photo inserts in
    # scene mode are added later and get the reel-wide transition.
    transitions = resolve_transitions(config, len(planned_durations), per_cut)
    xfade_durs = [d for _, d in transitions]

    # Beat sync: analyze the chosen track (tempo + phase) and compute the
    # per-clip end trims that put each crossfade midpoint on a beat. Must
    # happen before clip extraction — the trims change clip bounds.
    end_trims: list[float] | None = None
    beat_grid = None
    if (
        track is not None
        and config.beat_sync
        and len(planned_durations) > 1
        and config.transition.kind != "cut"
    ):
        from reelforge_core.compose.beats import compute_beat_end_trims, detect_beats

        beat_grid = await asyncio.to_thread(detect_beats, Path(track.path))
        if beat_grid is not None:
            end_trims = compute_beat_end_trims(
                planned_durations,
                xfade_durs,
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
        if timeline is not None:
            from reelforge_core.compose.clips import extract_timeline_clips

            clips = await extract_timeline_clips(
                timeline.shots,
                sources,
                analyses,
                config,
                reel_dir,
                log_file,
                _clip_progress,
                end_trims=end_trims,
            )
        else:
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
    except FileNotFoundError as exc:
        raise ComposeError(str(exc)) from exc

    # Still photos become shots of their own (scene mode only — a timeline
    # already carries its photos as shots), spliced into the sequence at
    # their configured positions.
    if timeline is None and config.photo_inserts:
        from reelforge_core.compose.photos import (
            interleave_photo_clips,
            render_photo_clip,
        )

        photos_dir = reel_dir / "clips"
        photos_dir.mkdir(parents=True, exist_ok=True)
        rendered: list[tuple[int, ClipInfo]] = []
        for i, insert in enumerate(config.photo_inserts):
            out_path = photos_dir / f"photo_{i:04d}.mp4"
            try:
                await render_photo_clip(insert, out_path, config, log_file, pan_index=i)
            except FileNotFoundError as exc:
                raise ComposeError(str(exc)) from exc
            except FFmpegError as exc:
                raise ComposeError(
                    f"could not render photo {insert.path}: {exc}"
                ) from exc
            rendered.append(
                (
                    insert.position,
                    ClipInfo(
                        path=out_path,
                        scene_index=-1,
                        in_ts=0.0,
                        out_ts=insert.duration_sec,
                        duration=insert.duration_sec,
                        has_audio=True,  # silent bed, but a real audio stream
                        effects_applied=["ken_burns"] if insert.ken_burns else [],
                        is_photo=True,
                        photo_asset_id=insert.asset_id,
                    ),
                )
            )
        clips = interleave_photo_clips(clips, rendered)
        log.info("composed with %d photo insert(s)", len(rendered))
        # The shot count changed; recompute per-cut transitions (reel-wide
        # default for every cut — photos never carry overrides).
        transitions = resolve_transitions(config, len(clips))
        xfade_durs = [d for _, d in transitions]

    await progress(_emit("clips", 1.0))

    # ----- captions -----
    await progress(_emit("captions", 0.0))
    # Voiceover takes get their own captions: transcribe each unmuted take
    # (cached per take under working/{take_asset_id}) before building the
    # subtitle track.
    voiceover_captions: list[tuple[object, object]] = []
    if (
        timeline is not None
        and timeline.voiceovers
        and config.captions.mode != "off"
        and config.captions.caption_voiceover
    ):
        from reelforge_core.analysis.transcribe import ensure_take_transcript

        live_takes = [t for t in timeline.voiceovers if t.effective_gain > 0 and t.path]
        for i, take in enumerate(live_takes):
            await progress(
                _emit("captions", 0.1 + 0.7 * (i / max(1, len(live_takes))),
                      f"transcribing voiceover {i + 1}/{len(live_takes)}")
            )
            try:
                transcript = await asyncio.to_thread(
                    ensure_take_transcript,
                    Path(take.path),
                    take.asset_id,
                    data_dir,
                    config.voiceover_whisper_model,
                )
            except Exception:
                log.warning("voiceover transcription failed for %s; no captions for it",
                            take.asset_id, exc_info=True)
                continue
            if transcript is not None:
                voiceover_captions.append((take, transcript))
    try:
        captions_path = build_captions(
            reel,
            analysis,
            config,
            reel_dir,
            end_trims=end_trims,
            clips=clips,
            analyses=analyses,
            overlays=list(timeline.overlays) if timeline is not None else [],
            xfades=xfade_durs,
            voiceover_captions=voiceover_captions,
        )
    except Exception as exc:
        raise ComposeError(f"caption build failed: {exc}") from exc
    # Burn the subtitle track in when it carries anything at all — spoken
    # captions and/or editor text overlays.
    captions_for_render: Path | None = (
        captions_path if has_dialogue(captions_path) else None
    )
    await progress(_emit("captions", 1.0))

    # ----- music -----
    await progress(_emit("music", 0.0))
    music_path: Path | None = None
    target_duration = max(0.1, sum(c.duration for c in clips) - sum(xfade_durs))
    if track is not None:
        try:
            music_path = prepare_music(track, target_duration, config, reel_dir, log_file)
        except FFmpegError as exc:
            raise ComposeError(f"music prep failed: {exc}") from exc
    await progress(_emit("music", 1.0))

    # ----- render -----
    await progress(_emit("render", 0.0))
    mezzanine_path = reel_dir / "mezzanine.mp4"
    voiceovers: list[tuple[Path, float, float]] = []
    if timeline is not None:
        for take in timeline.voiceovers:
            if take.effective_gain <= 0:
                continue
            vo_path = Path(take.path) if take.path else None
            if vo_path is None or not vo_path.exists():
                raise ComposeError(
                    f"voiceover take {take.label or take.asset_id} is missing on disk ({take.path!r})"
                )
            voiceovers.append((vo_path, max(0.0, take.start_sec), take.effective_gain))
    plan = build_final_command(
        clips=clips,
        analysis=analysis,
        music_path=music_path,
        captions_path=captions_for_render,
        config=config,
        output_path=mezzanine_path,
        transitions=transitions,
        voiceovers=voiceovers,
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
                **(
                    {"kind": "photo", "photo_asset_id": c.photo_asset_id}
                    if c.is_photo
                    else {}
                ),
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
