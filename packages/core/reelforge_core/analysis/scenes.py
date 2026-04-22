"""Scene detection (PySceneDetect) + short-scene merge + parallel thumbnail extraction."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from reelforge_core.errors import SceneDetectionError
from reelforge_core.ingest import MediaAsset
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import AnalysisConfig, ProgressCallback, ProgressEvent, Scene, compute_overall

log = logging.getLogger(__name__)

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
HDR_THRESHOLD = 40.0
_DEFAULT_THRESHOLD = 27.0


def _detect_raw(source: Path, threshold: float) -> list[tuple[float, float]]:
    # Imported lazily so unit tests that don't need scenedetect can still import scenes.py.
    from scenedetect import ContentDetector, detect

    raw = detect(str(source), ContentDetector(threshold=threshold))
    return [(t[0].get_seconds(), t[1].get_seconds()) for t in raw]


def merge_short_scenes(
    intervals: list[tuple[float, float]], min_duration: float
) -> list[tuple[float, float]]:
    """Merge any scene shorter than `min_duration` into its shorter neighbor.

    Re-indexed caller-side. Exposed as a pure function for unit testing.
    """
    if not intervals:
        return []
    merged = list(intervals)
    i = 0
    while i < len(merged):
        start, end = merged[i]
        if end - start >= min_duration or len(merged) == 1:
            i += 1
            continue
        left = merged[i - 1] if i > 0 else None
        right = merged[i + 1] if i + 1 < len(merged) else None
        if left is None:
            # only a right neighbor: merge right into current
            merged[i] = (start, right[1])
            del merged[i + 1]
        elif right is None:
            merged[i - 1] = (left[0], end)
            del merged[i]
            i = max(0, i - 1)
        else:
            # pick the shorter neighbor so we don't stretch the longer one
            left_dur = left[1] - left[0]
            right_dur = right[1] - right[0]
            if left_dur <= right_dur:
                merged[i - 1] = (left[0], end)
                del merged[i]
                i = max(0, i - 1)
            else:
                merged[i] = (start, right[1])
                del merged[i + 1]
        # keep re-checking from i; may still be short after merge
    return merged


def _extract_thumb(source: Path, out_path: Path, midpoint_sec: float, width: int) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{midpoint_sec:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "3",
        "-y",
        str(out_path),
    ]
    log.debug("ffmpeg thumb: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not out_path.exists():
        raise SceneDetectionError(
            f"thumbnail extraction failed for {out_path.name}: {result.stderr.strip()}"
        )


async def detect_scenes(
    asset: MediaAsset,
    working_dir: Path,
    config: AnalysisConfig,
    progress: ProgressCallback,
) -> list[Scene]:
    thumbs_dir = working_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    threshold = config.scene_threshold
    transfer = asset.probe.color_transfer
    if (
        transfer in HDR_TRANSFERS
        and abs(threshold - _DEFAULT_THRESHOLD) < 1e-6
    ):
        log.warning(
            "HDR source detected (transfer=%s); bumping scene threshold %.1f -> %.1f",
            transfer,
            threshold,
            HDR_THRESHOLD,
        )
        threshold = HDR_THRESHOLD

    # PySceneDetect is sync + CPU-bound; run in a thread so we don't block the loop.
    try:
        intervals = await asyncio.to_thread(_detect_raw, asset.path, threshold)
    except Exception as exc:  # pragma: no cover - defensive
        raise SceneDetectionError(f"PySceneDetect failed: {exc}") from exc

    if not intervals:
        log.info("no scenes detected; emitting synthetic full-clip scene")
        intervals = [(0.0, asset.probe.duration_s)]

    intervals = merge_short_scenes(intervals, config.min_scene_duration)

    scenes: list[Scene] = []
    for i, (start, end) in enumerate(intervals):
        scenes.append(
            Scene(
                index=i,
                start_sec=start,
                end_sec=end,
                start_frame=int(round(start * asset.fps)),
                end_frame=int(round(end * asset.fps)),
                thumbnail_path=f"thumbs/scene_{i:04d}.jpg",
            )
        )

    # Parallel thumbnail extraction, bounded concurrency.
    total = len(scenes)
    done_count = 0
    sem = asyncio.Semaphore(4)
    await progress(ProgressEvent("scenes", 0.05, compute_overall("scenes", 0.05)))

    async def _run_one(s: Scene) -> None:
        midpoint = (s.start_sec + s.end_sec) / 2
        out_path = working_dir / s.thumbnail_path
        async with sem:
            await asyncio.to_thread(
                _extract_thumb, asset.path, out_path, midpoint, config.thumbnail_width
            )

    tasks = [asyncio.create_task(_run_one(s)) for s in scenes]
    for coro in asyncio.as_completed(tasks):
        await coro
        done_count += 1
        frac = done_count / total if total else 1.0
        await progress(ProgressEvent("scenes", frac, compute_overall("scenes", frac)))

    write_json_atomic(working_dir / "scenes.json", [s.model_dump() for s in scenes])
    await progress(ProgressEvent("scenes", 1.0, compute_overall("scenes", 1.0)))
    return scenes
