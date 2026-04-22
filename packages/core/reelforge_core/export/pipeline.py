"""Export orchestrator: locate mezzanine → skip-if-exists → transcode → verify → sidecar."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from reelforge_core.compose.graph import ffmpeg_version
from reelforge_core.errors import (
    ExportError,
    FFmpegError,
    MezzanineNotFoundError,
    OutputVerificationError,
)
from reelforge_core.export.command import build_export_command
from reelforge_core.export.presets import PRESET_SPEC_VERSION, get_preset
from reelforge_core.export.verify import sanity_check_size, verify_export
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    REELFORGE_VERSION,
    ComposeManifest,
    ExportManifest,
    ProgressEvent,
)

log = logging.getLogger(__name__)

STAGE_WEIGHTS: dict[str, float] = {
    "prepare": 0.05,
    "transcode": 0.90,
    "finalize": 0.05,
}

FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):([\d.]+)")

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


async def _noop(_: ProgressEvent) -> None:
    return None


def _overall(stage: str, sp: float) -> float:
    total = 0.0
    for s, w in STAGE_WEIGHTS.items():
        if s == stage:
            total += w * max(0.0, min(1.0, sp))
            break
        total += w
    return min(1.0, total)


def _emit(stage: str, sp: float, message: str | None = None) -> ProgressEvent:
    return ProgressEvent(stage=stage, stage_progress=sp, overall_progress=_overall(stage, sp), message=message)  # type: ignore[arg-type]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _paths(asset_id: str, reel_id: str) -> tuple[Path, Path]:
    data_dir = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    reel_dir = data_dir / "working" / asset_id / "reels" / reel_id
    out_dir = data_dir / "outputs" / asset_id / reel_id
    return reel_dir, out_dir


async def _run_with_progress(
    args: list[str],
    *,
    total_duration_sec: float,
    progress: ProgressCallback,
) -> None:
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
                sp = (
                    max(0.0, min(1.0, t / total_duration_sec))
                    if total_duration_sec > 0
                    else 1.0
                )
                now = time.monotonic()
                if now - last_emit >= 0.5 or sp >= 1.0:
                    last_emit = now
                    await progress(_emit("transcode", sp))
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
            f"ffmpeg (export) exited {rc}", stderr=tail, cmdline=" ".join(args)
        )


def _load_existing_manifest(path: Path) -> ExportManifest | None:
    if not path.exists():
        return None
    try:
        return ExportManifest.model_validate_json(path.read_text())
    except Exception as exc:
        log.warning("failed to parse existing sidecar %s: %s", path, exc)
        return None


async def export(
    asset_id: str,
    reel_id: str,
    preset_id: str,
    *,
    force: bool = False,
    progress: ProgressCallback = _noop,
) -> ExportManifest:
    t_start = time.monotonic()

    await progress(_emit("prepare", 0.0))
    reel_dir, out_dir = _paths(asset_id, reel_id)
    mezzanine = reel_dir / "mezzanine.mp4"
    compose_json = reel_dir / "compose.json"
    if not mezzanine.exists():
        raise MezzanineNotFoundError(
            f"mezzanine not found for reel {reel_id}: {mezzanine}. Run compose first."
        )

    preset = get_preset(preset_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{preset_id}.{preset.container}"
    sidecar_path = out_dir / f"{preset_id}.export.json"

    # Mezzanine hash — small files, sub-second even for 100s of MB.
    mezz_hash = await asyncio.to_thread(_sha256_file, mezzanine)

    mezzanine_duration: float | None = None
    if compose_json.exists():
        try:
            manifest = ComposeManifest.model_validate_json(compose_json.read_text())
            mezzanine_duration = manifest.duration_sec
        except Exception as exc:
            log.warning("unable to parse compose.json for %s: %s", reel_id, exc)

    # Skip-if-exists: prior sidecar with matching hash + preset_spec_version is fine.
    if not force:
        existing = _load_existing_manifest(sidecar_path)
        if (
            existing is not None
            and existing.input_mezzanine_sha256 == mezz_hash
            and existing.preset_spec_version == PRESET_SPEC_VERSION
            and output_path.exists()
            and output_path.stat().st_size > 0
        ):
            log.info("export %s already current for reel %s; skipping", preset_id, reel_id)
            await progress(_emit("prepare", 1.0))
            await progress(_emit("transcode", 1.0))
            await progress(_emit("finalize", 1.0))
            return existing

    cmd = build_export_command(mezzanine, output_path, preset)
    await progress(_emit("prepare", 1.0))

    # Write the command to the mezzanine's log too (nice for debugging).
    log_path = reel_dir / "ffmpeg_commands.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        import shlex

        f.write(" ".join(shlex.quote(a) for a in cmd) + "\n\n")

    await progress(_emit("transcode", 0.0))
    try:
        await _run_with_progress(
            cmd,
            total_duration_sec=mezzanine_duration or 0.0,
            progress=progress,
        )
    except FFmpegError as exc:
        raise ExportError(f"export transcode failed: {exc}") from exc
    await progress(_emit("transcode", 1.0))

    await progress(_emit("finalize", 0.0))
    try:
        verified = verify_export(
            output_path, preset, mezzanine_duration_sec=mezzanine_duration
        )
    except OutputVerificationError:
        # Spec: do NOT delete broken output; keep for inspection.
        raise

    # Warn on wild size drift from the typical ratio (not a failure).
    try:
        sanity_check_size(
            output_size_bytes=verified.file_size_bytes,
            mezzanine_size_bytes=mezzanine.stat().st_size,
            preset=preset,
        )
    except Exception:  # pragma: no cover
        pass

    manifest = ExportManifest(
        asset_id=asset_id,
        reel_id=reel_id,
        preset_id=preset_id,  # type: ignore[arg-type]
        preset_spec_version=PRESET_SPEC_VERSION,
        output_path=str(output_path),
        input_mezzanine_path=str(mezzanine),
        input_mezzanine_sha256=mezz_hash,
        container=preset.container,
        video_codec=verified.video_codec,
        video_pixel_format=verified.pixel_format,
        audio_codec=verified.audio_codec,
        duration_sec=round(verified.duration_sec, 3),
        width=verified.width,
        height=verified.height,
        fps=verified.fps,
        file_size_bytes=verified.file_size_bytes,
        ffmpeg_version=ffmpeg_version(),
        ffmpeg_command=cmd,
        reelforge_version=REELFORGE_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=round(time.monotonic() - t_start, 3),
    )
    write_json_atomic(sidecar_path, json.loads(manifest.model_dump_json()))
    await progress(_emit("finalize", 1.0))
    return manifest
