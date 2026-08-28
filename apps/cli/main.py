"""ReelForge CLI. Phase 1: `probe`, `analyze`. Phase 2: `select`."""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import time as _time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from reelforge_core import (
    AnalysisConfig,
    AnalysisReport,
    CaptionStyle,
    ComposeConfig,
    ComposeManifest,
    EffectsConfig,
    ExportManifest,
    REELFORGE_VERSION,
    ReelSelection,
    SelectionConfig,
    TransitionStyle,
)
from reelforge_core import probe as probe_media

app = typer.Typer(add_completion=False, no_args_is_help=True, help="ReelForge command line.")
console = Console()


def _format_duration(sec: float) -> str:
    sec = max(0.0, sec)
    m, s = divmod(int(round(sec)), 60)
    if m < 60:
        return f"{m}:{s:02d} ({sec:.2f}s)"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d} ({sec:.2f}s)"


@app.command()
def version() -> None:
    """Print the ReelForge version."""
    console.print(f"reelforge {REELFORGE_VERSION}")


@app.command()
def probe(
    path: Path = typer.Argument(..., exists=False, help="Path to a media file inside /data."),
) -> None:
    """Print duration, resolution, fps, codecs, and content id for a media file."""
    if not path.exists():
        console.print(f"[red]not found:[/red] {path}")
        raise typer.Exit(code=2)
    asset = probe_media(path)
    p = asset.probe

    table = Table(title=f"probe: {asset.path}", show_header=False)
    table.add_row("id", asset.id)
    table.add_row("size (bytes)", f"{asset.size_bytes:,}")
    table.add_row("duration (s)", f"{p.duration_s:.3f}")
    table.add_row("resolution", f"{p.width}x{p.height}")
    table.add_row("fps", f"{p.fps:.3f}")
    table.add_row("video codec", p.video_codec)
    table.add_row("audio codec", p.audio_codec or "(none)")
    table.add_row("container", p.container)
    if p.bit_rate is not None:
        table.add_row("bit rate (bps)", f"{p.bit_rate:,}")
    console.print(table)


def _print_summary(report: AnalysisReport) -> None:
    scenes = report.scenes
    durations = [s.end_sec - s.start_sec for s in scenes] if scenes else []
    loudness_vals = [p.lufs for p in report.loudness if p.lufs > -79.9]

    lines: list[tuple[str, str]] = []
    lines.append(("Asset", report.asset_id))
    lines.append(("Source", report.source_path))
    lines.append(("Duration", _format_duration(report.duration)))
    lines.append(
        ("Resolution", f"{report.width}x{report.height} @ {report.fps:.2f} fps")
    )
    lines.append(("Audio", "yes" if report.has_audio else "no"))
    if scenes:
        lines.append(
            (
                "Scenes",
                f"{len(scenes)} (avg {statistics.mean(durations):.1f}s, "
                f"shortest {min(durations):.1f}s, longest {max(durations):.1f}s)",
            )
        )
    else:
        lines.append(("Scenes", "0"))
    if report.transcript is not None:
        word_count = sum(len(s.words) for s in report.transcript.segments)
        lines.append(
            (
                "Transcript",
                f"{report.transcript.language} ({report.transcript.language_probability:.2f}), "
                f"{word_count} words in {len(report.transcript.segments)} segments",
            )
        )
    else:
        lines.append(("Transcript", "(none — silent or no audio track)"))
    if report.loudness:
        mean = statistics.mean(loudness_vals) if loudness_vals else float("-inf")
        p95_src = sorted(loudness_vals) if loudness_vals else [float("-inf")]
        p95 = p95_src[max(0, int(0.95 * len(p95_src)) - 1)] if p95_src else float("-inf")
        lines.append(
            (
                "Loudness bins",
                f"{len(report.loudness)} points, mean {mean:.1f} LUFS, p95 {p95:.1f} LUFS",
            )
        )
    else:
        lines.append(("Loudness bins", "0 (no audio)"))
    cached = sum(1 for s in report.semantics if s.cached)
    lines.append(
        (
            "Semantics",
            f"{len(report.semantics)} scenes tagged "
            f"({cached} via cache, {len(report.semantics) - cached} new calls)",
        )
    )
    u = report.anthropic_usage
    lines.append(
        (
            "Anthropic usage",
            f"input {u.get('input_tokens', 0):,} / output {u.get('output_tokens', 0):,} tokens",
        )
    )
    lines.append(("Elapsed", _format_duration(report.elapsed_sec)))
    lines.append(
        (
            "Output",
            str(
                Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
                / "working"
                / report.asset_id
                / "analysis.json"
            ),
        )
    )

    table = Table(title="ReelForge Analysis Report", show_header=False, expand=False)
    for k, v in lines:
        table.add_row(k + ":", v)
    console.print(table)


async def _print_progress(evt) -> None:
    console.log(
        f"[{evt.stage}] stage={evt.stage_progress * 100:5.1f}% "
        f"overall={evt.overall_progress * 100:5.1f}%"
        + (f" — {evt.message}" if evt.message else "")
    )


async def _run_local(path: Path, config: AnalysisConfig) -> AnalysisReport:
    from reelforge_core.analysis import analyze

    asset = probe_media(path)
    return await analyze(asset, config, progress=_print_progress)


async def _run_queued(path: Path, config: AnalysisConfig) -> AnalysisReport:
    import arq
    from arq.connections import RedisSettings

    redis_url = os.environ["REDIS_URL"]
    asset = probe_media(path)
    # Pre-write probe.json so the worker knows which path to re-probe.
    from reelforge_core.analysis.pipeline import working_dir_for
    from reelforge_core.io_utils import write_json_atomic
    from reelforge_core.ingest import asset_to_dict

    write_json_atomic(working_dir_for(asset.id) / "probe.json", asset_to_dict(asset))

    pool = await arq.create_pool(RedisSettings.from_dsn(redis_url))
    job = await pool.enqueue_job("analyze_asset", asset.id, config.model_dump())
    console.log(f"enqueued job {job.job_id} for asset {asset.id}")

    # Tail progress until completion.
    import redis.asyncio as redis_async

    r = redis_async.from_url(redis_url, decode_responses=True)
    seen_overall = -1.0
    try:
        while True:
            data = await r.hgetall(f"job:{job.job_id}:progress")
            if data:
                stage = data.get("stage", "?")
                stage_p = float(data.get("stage_progress", 0) or 0)
                overall = float(data.get("overall", 0) or 0)
                if overall > seen_overall:
                    console.log(
                        f"[{stage}] stage={stage_p * 100:5.1f}% overall={overall * 100:5.1f}%"
                    )
                    seen_overall = overall
                if stage in {"done", "error"}:
                    break
            try:
                status = await job.status()
            except Exception:
                status = None
            if str(status) in {"JobStatus.complete", "complete"}:
                break
            if str(status) in {"JobStatus.not_found", "not_found"}:
                break
            await asyncio.sleep(0.5)
        result = await job.result(timeout=3600)
    finally:
        await r.aclose()
        await pool.aclose()

    analysis_path = Path(result["analysis_path"])
    return AnalysisReport.model_validate_json(analysis_path.read_text())


@app.command()
def analyze(
    path: Path = typer.Argument(..., help="Path to a media file inside /data."),
    model: str = typer.Option("base.en", "--model", help="faster-whisper model name."),
    threshold: float = typer.Option(
        27.0, "--threshold", help="PySceneDetect ContentDetector threshold."
    ),
    aspect: str = typer.Option(
        None, "--aspect", help="(reserved for Phase 3)", show_default=False
    ),
    resume: bool = typer.Option(False, "--resume/--no-resume", help="Skip completed stages."),
    local: bool = typer.Option(
        False,
        "--local/--queued",
        help="Run in-process (--local) or enqueue to the worker (--queued, default).",
    ),
) -> None:
    """Analyze a media file: scenes + transcript + loudness + semantics → analysis.json."""
    if not path.exists():
        console.print(f"[red]not found:[/red] {path}")
        raise typer.Exit(code=2)

    config = AnalysisConfig(
        scene_threshold=threshold,
        whisper_model=model,
        resume=resume,
    )

    try:
        if local:
            report = asyncio.run(_run_local(path, config))
        else:
            report = asyncio.run(_run_queued(path, config))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(code=130)
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)

    _print_summary(report)


# ---------------------------------------------------------------------------
# Phase 2: select
# ---------------------------------------------------------------------------


ASSET_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _data_dir() -> Path:
    return Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))


def _resolve_asset_id(arg: str) -> str:
    """Map the CLI argument to an asset_id.

    - If it's a 64-hex string, treat as an asset_id directly.
    - Else treat as a path; if the file exists, probe it to derive the id.
    """
    if ASSET_ID_RE.match(arg):
        return arg
    p = Path(arg)
    if not p.exists():
        raise typer.BadParameter(
            f"'{arg}' is neither a 64-hex asset_id nor an existing file path"
        )
    asset = probe_media(p)
    return asset.id


def _format_timespan(start: float, end: float) -> str:
    def fmt(s: float) -> str:
        m, s = divmod(int(round(s)), 60)
        return f"{m}:{s:02d}"

    return f"{fmt(start)} – {fmt(end)}"


def _print_selection_summary(selection: ReelSelection) -> None:
    header = Table(show_header=False, box=None, pad_edge=False)
    header.add_row("Asset:", selection.asset_id)
    header.add_row(
        "Candidates:",
        f"{selection.candidates_generated} generated • "
        f"{selection.candidates_dropped_by_dedup} dropped to dedup • "
        f"kept top {len(selection.reels)}",
    )
    header.add_row("Model:", selection.config.ranking_model)
    u = selection.anthropic_usage
    header.add_row(
        "Tokens:",
        f"in {u.get('input_tokens', 0):,} / out {u.get('output_tokens', 0):,}",
    )
    header.add_row("Elapsed:", f"{selection.elapsed_sec:.2f}s")
    console.print(header)

    if not selection.reels:
        console.print(
            "[yellow]No 30-60s spans available in this source.[/yellow] "
            "Consider shortening scenes (lower threshold) or use a longer source video."
        )
        return

    show_match = any(r.prompt_relevance is not None for r in selection.reels)
    table = Table(title="ReelForge Selection", show_lines=False)
    table.add_column("#", justify="right", style="bold")
    table.add_column("Timespan")
    table.add_column("Dur", justify="right")
    table.add_column("Title")
    table.add_column("Hook", overflow="fold")
    table.add_column("Mood")
    if show_match:
        table.add_column("Match", justify="right")
    table.add_column("Score", justify="right")
    for reel in selection.reels:
        row = [
            str(reel.rank),
            _format_timespan(reel.start_sec, reel.end_sec),
            f"{reel.duration_sec:.1f}s",
            reel.title,
            reel.hook,
            reel.suggested_mood,
        ]
        if show_match:
            row.append(
                f"{reel.prompt_relevance}%" if reel.prompt_relevance is not None else "—"
            )
        row.append(f"{reel.overall:.1f}")
        table.add_row(*row)
    console.print(table)


async def _progress_tail(redis_url: str, job_id: str) -> None:
    import redis.asyncio as redis_async

    r = redis_async.from_url(redis_url, decode_responses=True)
    seen = -1.0
    try:
        while True:
            data = await r.hgetall(f"job:{job_id}:progress")
            if data:
                stage = data.get("stage", "?")
                overall = float(data.get("overall", 0) or 0)
                stage_p = float(data.get("stage_progress", 0) or 0)
                if overall > seen:
                    console.log(
                        f"[{stage}] stage={stage_p * 100:5.1f}% overall={overall * 100:5.1f}%"
                    )
                    seen = overall
                if stage in {"done", "error"}:
                    break
            await asyncio.sleep(0.5)
    finally:
        await r.aclose()


async def _select_local(asset_id: str, config: SelectionConfig) -> ReelSelection:
    from reelforge_core.reels import select_reels

    analysis_path = _data_dir() / "working" / asset_id / "analysis.json"
    if not analysis_path.exists():
        raise FileNotFoundError(f"analysis.json not found at {analysis_path}")
    analysis = AnalysisReport.model_validate_json(analysis_path.read_text())

    async def _log_progress(evt) -> None:
        console.log(
            f"[{evt.stage}] stage={evt.stage_progress * 100:5.1f}% "
            f"overall={evt.overall_progress * 100:5.1f}%"
            + (f" — {evt.message}" if evt.message else "")
        )

    return await select_reels(analysis, config, progress=_log_progress)


async def _select_queued(asset_id: str, config: SelectionConfig) -> ReelSelection:
    import arq
    from arq.connections import RedisSettings

    redis_url = os.environ["REDIS_URL"]
    pool = await arq.create_pool(RedisSettings.from_dsn(redis_url))
    try:
        job = await pool.enqueue_job(
            "select_reels_job", asset_id, config.model_dump()
        )
        console.log(f"enqueued job {job.job_id} for asset {asset_id}")
        tail = asyncio.create_task(_progress_tail(redis_url, job.job_id))
        try:
            result = await job.result(timeout=900)
        finally:
            tail.cancel()
            try:
                await tail
            except asyncio.CancelledError:
                pass
    finally:
        await pool.aclose()

    reels_path = Path(result["reels_path"])
    return ReelSelection.model_validate_json(reels_path.read_text())


@app.command()
def select(
    target: str = typer.Argument(
        ..., help="Asset id (64 hex chars) or path to a source video."
    ),
    top: int = typer.Option(10, "--top", help="Maximum reels to return after dedup."),
    min_sec: float = typer.Option(30.0, "--min-sec", help="Minimum reel duration."),
    max_sec: float = typer.Option(60.0, "--max-sec", help="Maximum reel duration."),
    max_scenes: int = typer.Option(
        6, "--max-scenes", help="Max scenes per reel candidate."
    ),
    overlap: float = typer.Option(
        0.5, "--overlap", help="Dedup overlap threshold (strict <)."
    ),
    model: str = typer.Option(
        "claude-sonnet-4-5", "--model", help="Anthropic model for ranking."
    ),
    resume: bool = typer.Option(
        False, "--resume/--no-resume", help="Reuse prior ranking if the stamp matches."
    ),
    prompt: str = typer.Option(
        None,
        "--prompt",
        help="Natural-language direction, e.g. 'clips of falls' or 'make it feel intense'.",
    ),
    local: bool = typer.Option(
        False,
        "--local/--queued",
        help="Run in-process (--local) or enqueue to the worker (--queued, default).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the full ReelSelection JSON instead of a table."
    ),
) -> None:
    """Rank 30-60s reel candidates from a completed analysis.json → reels.json."""
    try:
        asset_id = _resolve_asset_id(target)
    except typer.BadParameter as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    config = SelectionConfig(
        target_min_sec=min_sec,
        target_max_sec=max_sec,
        max_scenes_per_reel=max_scenes,
        top_k=top,
        overlap_threshold=overlap,
        ranking_model=model,
        resume=resume,
        prompt=prompt,
    )

    try:
        if local:
            selection = asyncio.run(_select_local(asset_id, config))
        else:
            selection = asyncio.run(_select_queued(asset_id, config))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(code=130)
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(selection.model_dump_json(indent=2))
    else:
        _print_selection_summary(selection)


# ---------------------------------------------------------------------------
# Phase 3: compose
# ---------------------------------------------------------------------------


def _resolve_reel_selector(selection: ReelSelection, reel_ref: str) -> str:
    """Map --reel arg (candidate_id or 1-indexed rank) to a candidate_id."""
    # Integer rank?
    try:
        rank = int(reel_ref)
    except ValueError:
        rank = None
    if rank is not None:
        match = next((r for r in selection.reels if r.rank == rank), None)
        if match is None:
            raise typer.BadParameter(
                f"no reel with rank {rank} (have {len(selection.reels)} reels)"
            )
        return match.candidate_id
    # Candidate id
    match = next((r for r in selection.reels if r.candidate_id == reel_ref), None)
    if match is None:
        raise typer.BadParameter(
            f"no reel with candidate_id {reel_ref!r} in selection"
        )
    return match.candidate_id


def _print_compose_summary(manifest: ComposeManifest) -> None:
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_row("Title:", manifest.reel_title)
    t.add_row("Hook:", manifest.reel_hook)
    t.add_row(
        "Scenes:",
        str([m["scene_index"] for m in manifest.scene_clip_map]),
    )
    t.add_row(
        "Span:",
        f"{manifest.duration_sec:.2f}s at "
        f"{manifest.width}x{manifest.height} @ {manifest.fps:.0f}fps",
    )
    if manifest.chosen_music:
        m = manifest.chosen_music
        bpm = f"{m.bpm} BPM, " if m.bpm else ""
        t.add_row(
            "Music:",
            f"{m.id} ({m.mood}, {bpm}{m.license})",
        )
    else:
        t.add_row("Music:", "(none)")
    t.add_row("Output:", manifest.mezzanine_path)
    t.add_row("Elapsed:", _format_duration(manifest.elapsed_sec))
    console.print(t)


async def _compose_local(
    asset_id: str, reel_id: str, config: ComposeConfig
) -> ComposeManifest:
    from reelforge_core.compose import compose as compose_fn

    wd = _data_dir() / "working" / asset_id
    analysis = AnalysisReport.model_validate_json((wd / "analysis.json").read_text())
    selection = ReelSelection.model_validate_json((wd / "reels.json").read_text())
    reel = next((r for r in selection.reels if r.candidate_id == reel_id), None)
    if reel is None:
        raise RuntimeError(f"reel {reel_id} not found")

    probe_path = wd / "probe.json"
    raw = json.loads(probe_path.read_text())
    asset = probe_media(Path(raw["path"]))

    async def _log_progress(evt) -> None:
        console.log(
            f"[{evt.stage}] stage={evt.stage_progress * 100:5.1f}% "
            f"overall={evt.overall_progress * 100:5.1f}%"
            + (f" — {evt.message}" if evt.message else "")
        )

    return await compose_fn(asset, reel, analysis, config, progress=_log_progress)


async def _compose_queued(
    asset_id: str, reel_id: str, config: ComposeConfig
) -> ComposeManifest:
    import arq
    from arq.connections import RedisSettings

    redis_url = os.environ["REDIS_URL"]
    pool = await arq.create_pool(RedisSettings.from_dsn(redis_url))
    try:
        job = await pool.enqueue_job(
            "compose_reel_job", asset_id, reel_id, config.model_dump()
        )
        console.log(f"enqueued job {job.job_id} for reel {reel_id}")
        tail = asyncio.create_task(_progress_tail(redis_url, job.job_id))
        try:
            result = await job.result(timeout=3600)
        finally:
            tail.cancel()
            try:
                await tail
            except asyncio.CancelledError:
                pass
    finally:
        await pool.aclose()

    compose_json_path = Path(result["compose_json_path"])
    return ComposeManifest.model_validate_json(compose_json_path.read_text())


@app.command()
def compose(
    target: str = typer.Argument(
        ..., help="Asset id (64 hex) or path to a source video."
    ),
    reel: str = typer.Option(
        ..., "--reel", help="Reel candidate_id or 1-indexed rank from reels.json."
    ),
    aspect: str = typer.Option("9:16", "--aspect"),
    fps: int = typer.Option(30, "--fps"),
    caption_mode: str = typer.Option("static", "--caption-mode"),
    transition: str = typer.Option("fade", "--transition"),
    transition_duration: float = typer.Option(0.4, "--transition-duration"),
    music_track: str = typer.Option(
        None, "--music-track", help="Specific track id (overrides auto mood match)."
    ),
    no_music: bool = typer.Option(False, "--no-music"),
    lut: str = typer.Option(None, "--lut"),
    no_effects: bool = typer.Option(False, "--no-effects"),
    crf: int = typer.Option(18, "--crf"),
    preset: str = typer.Option("medium", "--preset"),
    local: bool = typer.Option(
        False, "--local/--queued", help="Run in-process or enqueue to the worker."
    ),
) -> None:
    """Render a selected reel → mezzanine.mp4 (the canonical, re-encodable source)."""
    try:
        asset_id = _resolve_asset_id(target)
    except typer.BadParameter as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    wd = _data_dir() / "working" / asset_id
    reels_path = wd / "reels.json"
    if not reels_path.exists():
        console.print(
            f"[red]error:[/red] {reels_path} not found. Run `reelforge select` first."
        )
        raise typer.Exit(code=2)

    selection = ReelSelection.model_validate_json(reels_path.read_text())
    try:
        reel_id = _resolve_reel_selector(selection, reel)
    except typer.BadParameter as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    effects = EffectsConfig(ken_burns_on_low_energy=not no_effects, unsharp=not no_effects, lut=None if no_effects else lut)
    config = ComposeConfig(
        aspect=aspect,  # type: ignore[arg-type]
        target_fps=fps,
        video_crf=crf,
        video_preset=preset,
        captions=CaptionStyle(mode=caption_mode),  # type: ignore[arg-type]
        transition=TransitionStyle(kind=transition, duration_sec=transition_duration),  # type: ignore[arg-type]
        effects=effects,
        music_track_id=music_track,
        no_music=no_music,
    )

    try:
        if local:
            manifest = asyncio.run(_compose_local(asset_id, reel_id, config))
        else:
            manifest = asyncio.run(_compose_queued(asset_id, reel_id, config))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(code=130)
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)

    _print_compose_summary(manifest)


# ---------------------------------------------------------------------------
# Phase 4: export
# ---------------------------------------------------------------------------


PRESET_CHOICES = ["mp4_h264_social", "mp4_h265_hq", "mov_prores_422", "mov_prores_hq"]


def _format_bytes(n: int) -> str:
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"


def _print_export_summary(manifest: ExportManifest, mezzanine_size: int) -> None:
    ratio = manifest.file_size_bytes / mezzanine_size if mezzanine_size else 0
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_row("Preset:", manifest.preset_id)
    t.add_row(
        "Codec:",
        f"{manifest.video_codec} {manifest.video_pixel_format} ({manifest.container})",
    )
    t.add_row("Audio:", manifest.audio_codec)
    t.add_row("Output:", manifest.output_path)
    t.add_row(
        "Duration:",
        f"{manifest.duration_sec:.2f}s • {manifest.width}x{manifest.height} @ "
        f"{manifest.fps:.0f}fps",
    )
    t.add_row(
        "File size:",
        f"{_format_bytes(manifest.file_size_bytes)} ({ratio:.2f}x mezzanine)",
    )
    t.add_row("Elapsed:", _format_duration(manifest.elapsed_sec))
    console.print(t)


async def _export_local(
    asset_id: str, reel_id: str, preset_id: str, force: bool
) -> ExportManifest:
    from reelforge_core.export import export as export_fn

    async def _log_progress(evt) -> None:
        console.log(
            f"[{evt.stage}] stage={evt.stage_progress * 100:5.1f}% "
            f"overall={evt.overall_progress * 100:5.1f}%"
        )

    return await export_fn(
        asset_id, reel_id, preset_id, force=force, progress=_log_progress
    )


async def _export_queued(
    asset_id: str, reel_id: str, preset_id: str, force: bool
) -> ExportManifest:
    import arq
    from arq.connections import RedisSettings

    redis_url = os.environ["REDIS_URL"]
    pool = await arq.create_pool(RedisSettings.from_dsn(redis_url))
    try:
        job = await pool.enqueue_job(
            "export_reel_job", asset_id, reel_id, preset_id, force
        )
        console.log(f"enqueued job {job.job_id} for {preset_id}")
        tail = asyncio.create_task(_progress_tail(redis_url, job.job_id))
        try:
            result = await job.result(timeout=1800)
        finally:
            tail.cancel()
            try:
                await tail
            except asyncio.CancelledError:
                pass
    finally:
        await pool.aclose()
    sidecar = Path(result["sidecar_path"])
    return ExportManifest.model_validate_json(sidecar.read_text())


@app.command()
def export(
    target: str = typer.Argument(
        ..., help="Asset id (64 hex) or path to a source video."
    ),
    reel: str = typer.Option(
        ..., "--reel", help="Reel candidate_id or 1-indexed rank."
    ),
    preset: str = typer.Option(
        None,
        "--preset",
        help=f"Preset id. One of: {', '.join(PRESET_CHOICES)}",
    ),
    all_presets: bool = typer.Option(
        False, "--all", help="Export every preset sequentially."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-transcode even if output exists and is current."
    ),
    local: bool = typer.Option(
        False, "--local/--queued", help="Run in-process or enqueue to the worker."
    ),
) -> None:
    """Transcode the Phase 3 mezzanine into one (or all) delivery preset(s)."""
    try:
        asset_id = _resolve_asset_id(target)
    except typer.BadParameter as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    wd = _data_dir() / "working" / asset_id
    reels_path = wd / "reels.json"
    if not reels_path.exists():
        console.print(
            f"[red]error:[/red] {reels_path} not found. Run `reelforge select` first."
        )
        raise typer.Exit(code=2)
    selection = ReelSelection.model_validate_json(reels_path.read_text())
    try:
        reel_id = _resolve_reel_selector(selection, reel)
    except typer.BadParameter as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    mezz = wd / "reels" / reel_id / "mezzanine.mp4"
    if not mezz.exists():
        console.print(
            f"[red]error:[/red] {mezz} not found. Run `reelforge compose` first."
        )
        raise typer.Exit(code=2)
    mezz_size = mezz.stat().st_size

    if not all_presets and not preset:
        console.print("[red]error:[/red] either --preset or --all is required")
        raise typer.Exit(code=2)
    if all_presets and preset:
        console.print("[red]error:[/red] --preset and --all are mutually exclusive")
        raise typer.Exit(code=2)
    if preset and preset not in PRESET_CHOICES:
        console.print(
            f"[red]error:[/red] unknown preset {preset!r}. Valid: {PRESET_CHOICES}"
        )
        raise typer.Exit(code=2)

    presets_to_run = PRESET_CHOICES if all_presets else [preset]

    runner = _export_local if local else _export_queued
    if all_presets:
        console.print(
            f"[bold]ReelForge Export — reel {reel_id} → ALL presets[/bold]"
        )
        results: list[ExportManifest] = []
        t_total = _time.monotonic()
        for i, pid in enumerate(presets_to_run, 1):
            try:
                manifest = asyncio.run(runner(asset_id, reel_id, pid, force))
            except KeyboardInterrupt:
                console.print("[yellow]interrupted[/yellow]")
                raise typer.Exit(code=130)
            except Exception as exc:
                console.print(f"[red]error on {pid}:[/red] {exc}")
                raise typer.Exit(code=1)
            console.log(
                f"[{i}/{len(presets_to_run)}] {pid}  "
                f"{_format_bytes(manifest.file_size_bytes)}  "
                f"✓ {_format_duration(manifest.elapsed_sec)}"
            )
            results.append(manifest)
        console.print(
            f"Total elapsed: {_format_duration(_time.monotonic() - t_total)}\n"
            f"Outputs: {_data_dir()}/outputs/{asset_id}/{reel_id}/"
        )
        return

    try:
        manifest = asyncio.run(runner(asset_id, reel_id, presets_to_run[0], force))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(code=130)
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)
    _print_export_summary(manifest, mezz_size)


# ---------------------------------------------------------------------------
# Phase 7: cleanup
# ---------------------------------------------------------------------------


@app.command()
def cleanup(
    project: str = typer.Option(
        None, "--project", help="Project id; omit to operate globally."
    ),
    mode: str = typer.Option(
        "safe",
        "--mode",
        help="safe | working | outputs | all",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Enumerate what would be deleted without deleting."
    ),
) -> None:
    """Free disk space under /data. Safe mode deletes only tmp + part dirs."""
    if mode not in {"safe", "working", "outputs", "all"}:
        console.print(f"[red]error:[/red] unknown mode {mode!r}")
        raise typer.Exit(code=2)

    data = _data_dir()
    # Without a project filter, treat "safe" as clip/music/preview cache purge.
    if project is None:
        from reelforge_core import cache as file_cache

        if mode == "safe":
            total = 0
            for kind in ("clip", "music", "caption_preview"):
                n = file_cache.cache_size(kind)
                if dry_run:
                    console.print(f"  would purge {kind}: {n:,} bytes")
                    total += n
                else:
                    total += file_cache.purge_kind(kind)
            console.print(f"{'would free' if dry_run else 'freed'}: {total:,} bytes")
            return
        console.print(
            "[yellow]--mode values other than 'safe' require --project when no global project is targeted.[/yellow]"
        )
        raise typer.Exit(code=2)

    import urllib.request
    import urllib.error
    import json as _json

    base = os.environ.get("REELFORGE_API_URL", "http://api:8001")
    url = f"{base}/api/v1/projects/{project}/cleanup"
    payload = _json.dumps({"mode": mode}).encode()
    if dry_run:
        # Use the disk_usage endpoint to show what lives under this project.
        du_url = f"{base}/api/v1/projects/{project}/disk_usage"
        try:
            with urllib.request.urlopen(du_url, timeout=10) as resp:
                data_resp = _json.loads(resp.read())
            console.print(
                f"project {project} current usage: "
                f"{data_resp['total_bytes']:,} bytes\n"
                f"mode={mode} would delete the corresponding files "
                f"(run without --dry-run to confirm)."
            )
        except Exception as exc:
            console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=1)
        return

    req = urllib.request.Request(
        url, data=payload, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        console.print(f"[red]error:[/red] {exc.code} {exc.reason}")
        raise typer.Exit(code=1)
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(
        f"freed {body.get('bytes_freed', 0):,} bytes across "
        f"{len(body.get('removed', []))} paths"
    )


# ---------------------------------------------------------------------------
# Selection v2: eval harness
# ---------------------------------------------------------------------------


@app.command("eval-select")
def eval_select(
    labels: Path = typer.Option(
        None,
        "--labels",
        help="Directory of <asset_id>.json label files "
        "(default: tests/reels/eval/labels, mounted into the cli container).",
    ),
) -> None:
    """Score reels.json against hand-labeled picks (recall@3/5/10)."""
    from reelforge_core.reels.evaluate import evaluate_all, format_report

    if labels is None:
        for cand in (Path("/app/tests/reels/eval/labels"), Path("tests/reels/eval/labels")):
            if cand.is_dir():
                labels = cand
                break
    if labels is None or not labels.is_dir():
        console.print("[red]error:[/red] no labels directory found; pass --labels DIR")
        raise typer.Exit(code=2)
    console.print(f"labels: {labels}   data: {_data_dir()}\n")
    console.print(format_report(evaluate_all(labels, _data_dir())), highlight=False)


if __name__ == "__main__":
    app()
