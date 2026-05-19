"""Compile multiple already-composed mezzanines into a single longer mezzanine.

Used by long-form output. Each input is a Phase-3 `mezzanine.mp4` that has
already been rendered (captions burned, music ducked, effects applied). The
compile step is a single xfade-chain ffmpeg run that produces a new mezzanine
suitable for Phase-4 export.

Each "chapter" keeps its own music bed; we deliberately don't try to bridge
music tracks across chapters because the resulting jumps usually sound worse
than a clean transition between distinct beds.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from reelforge_core.compose.graph import FilterGraph, FilterNode, ffmpeg_version
from reelforge_core.errors import ComposeError, FFmpegError
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import REELFORGE_VERSION, ComposeManifest, ProgressEvent

log = logging.getLogger(__name__)

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


async def _noop(_: ProgressEvent) -> None:
    return None


def _probe_duration(path: Path) -> float:
    """Return container duration via ffprobe. Caller passes validated mezzanines."""
    import subprocess

    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise ComposeError(f"ffprobe failed on chapter {path}: {r.stderr.strip()}")
    data = json.loads(r.stdout)
    return float(data["format"]["duration"])


def build_montage_command(
    *,
    inputs: list[Path],
    output_path: Path,
    transition_duration: float = 0.6,
    transition_kind: str = "fade",
    target_resolution: tuple[int, int] = (1080, 1920),
    target_fps: int = 30,
    crf: int = 18,
    audio_bitrate_kbps: int = 256,
) -> tuple[list[str], float]:
    """Build the ffmpeg argv for an N-input xfade+acrossfade chain.

    Returns `(argv, total_duration_sec)`. Reuses the FilterGraph DSL so label
    uniqueness + escaping match the Phase-3 implementation. `transition_kind`
    is one of xfade's supported `transition=` values; pick via
    `compose/auto.py::pick_transition_kind_for_montage` for the smart default.
    """
    if not inputs:
        raise ComposeError("montage requires at least one input mezzanine")
    w, h = target_resolution

    durations = [_probe_duration(p) for p in inputs]
    # Each input is normalised so xfade sees matching timebases / SAR / sample rates.
    # -stats forces ffmpeg to emit `time=...` lines on stderr even when
    # -loglevel is warning — required for the render progress watcher.
    args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    for p in inputs:
        args += ["-i", str(p)]

    graph = FilterGraph()
    v_labels: list[str] = []
    a_labels: list[str] = []
    for i, _ in enumerate(inputs):
        # Match the Phase-3 PTS-reset trick so xfade has a consistent zero-aligned
        # timeline per input.
        graph.add(
            FilterNode(
                filter_name=(
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    "setsar=1,"
                    f"fps={target_fps},"
                    "format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS"
                ),
                inputs=[f"[{i}:v]"],
                outputs=[f"[v{i}]"],
            )
        )
        graph.add(
            FilterNode(
                filter_name=(
                    "aresample=48000,"
                    "aformat=sample_rates=48000:channel_layouts=stereo,"
                    "asetpts=PTS-STARTPTS"
                ),
                inputs=[f"[{i}:a]"],
                outputs=[f"[a{i}]"],
            )
        )
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")

    # xfade chain
    v_chain = v_labels[0]
    a_chain = a_labels[0]
    cumulative = 0.0
    for i in range(1, len(inputs)):
        cumulative += durations[i - 1] - transition_duration
        next_v = f"[xv{i}]"
        next_a = f"[xa{i}]"
        graph.add(
            FilterNode(
                filter_name="xfade",
                inputs=[v_chain, v_labels[i]],
                outputs=[next_v],
                args={
                    "transition": transition_kind,
                    "duration": f"{transition_duration:.3f}",
                    "offset": f"{cumulative:.3f}",
                },
            )
        )
        graph.add(
            FilterNode(
                filter_name="acrossfade",
                inputs=[a_chain, a_labels[i]],
                outputs=[next_a],
                args={"d": f"{transition_duration:.3f}"},
            )
        )
        v_chain = next_v
        a_chain = next_a

    # Final passthrough so the mapped labels are uniform across single/multi cases.
    graph.add(FilterNode(filter_name="null", inputs=[v_chain], outputs=["[vfinal]"]))
    graph.add(FilterNode(filter_name="anull", inputs=[a_chain], outputs=["[afinal]"]))

    total_duration = sum(durations) - max(0, len(durations) - 1) * transition_duration

    args += [
        "-filter_complex",
        graph.serialize(),
        "-map",
        "[vfinal]",
        "-map",
        "[afinal]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(target_fps),
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_bitrate_kbps}k",
        "-movflags",
        "+faststart",
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-shortest",
        str(output_path),
    ]
    return args, total_duration


async def compile_montage(
    *,
    inputs: list[Path],
    output_dir: Path,
    montage_id: str,
    transition_duration: float = 0.6,
    transition_kind: str = "fade",
    target_resolution: tuple[int, int] = (1080, 1920),
    target_fps: int = 30,
    progress: ProgressCallback = _noop,
) -> ComposeManifest:
    """Run the montage compile + write a Phase-3-shape manifest next to the output.

    `transition_kind` is one of xfade's `transition=` values. The worker passes
    the picker's choice (see `compose/auto.py::pick_transition_kind_for_montage`);
    callers that don't care can leave it at "fade".
    """
    import asyncio
    import re

    t_start = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "mezzanine.mp4"
    log_path = output_dir / "ffmpeg_commands.log"

    cmd, total_duration = build_montage_command(
        inputs=inputs,
        output_path=out_path,
        transition_duration=transition_duration,
        transition_kind=transition_kind,
        target_resolution=target_resolution,
        target_fps=target_fps,
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    import shlex

    with log_path.open("a", encoding="utf-8") as f:
        f.write(" ".join(shlex.quote(a) for a in cmd) + "\n\n")

    await progress(ProgressEvent(stage="render", stage_progress=0.0, overall_progress=0.0))  # type: ignore[arg-type]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    time_re = re.compile(r"time=(\d+):(\d+):([\d.]+)")
    stderr_buf: list[str] = []
    assert proc.stderr is not None
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace")
        stderr_buf.append(text)
        m = time_re.search(text)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2))
            s = float(m.group(3))
            t = h * 3600 + mi * 60 + s
            sp = min(1.0, t / total_duration) if total_duration else 1.0
            await progress(
                ProgressEvent(stage="render", stage_progress=sp, overall_progress=sp * 0.95)  # type: ignore[arg-type]
            )
        if len(stderr_buf) > 4000:
            del stderr_buf[:2000]

    rc = await proc.wait()
    if rc != 0:
        tail = "".join(stderr_buf)[-4000:]
        raise FFmpegError(
            f"montage compile exited {rc}", stderr=tail, cmdline=" ".join(cmd)
        )

    manifest = ComposeManifest(
        asset_id="",  # montage is project-level; no single asset
        reel_id=montage_id,
        reel_title="Montage",
        reel_hook="",
        config=__import__(
            "reelforge_core", fromlist=["ComposeConfig"]
        ).ComposeConfig(),  # placeholder; the real config lives on the parent project
        chosen_music=None,
        mezzanine_path=str(out_path),
        duration_sec=round(total_duration, 3),
        width=target_resolution[0],
        height=target_resolution[1],
        fps=float(target_fps),
        scene_clip_map=[{"chapter_index": i, "chapter_path": str(p)} for i, p in enumerate(inputs)],
        ffmpeg_version=ffmpeg_version(),
        reelforge_version=REELFORGE_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=round(time.monotonic() - t_start, 3),
    )
    write_json_atomic(output_dir / "compose.json", json.loads(manifest.model_dump_json()))
    await progress(
        ProgressEvent(stage="finalize", stage_progress=1.0, overall_progress=1.0)  # type: ignore[arg-type]
    )
    return manifest
