"""End-to-end analysis orchestration: probe → scenes → transcribe → loudness →
semantics → analysis.json. Resume-aware via per-step stamp files."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reelforge_core.analysis.audio import measure_loudness
from reelforge_core.analysis.scenes import detect_scenes
from reelforge_core.analysis.semantics import analyze_semantics
from reelforge_core.analysis.transcribe import transcribe
from reelforge_core.db import init_db
from reelforge_core.errors import AnalysisError
from reelforge_core.ingest import MediaAsset, asset_to_dict
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    REELFORGE_VERSION,
    AnalysisConfig,
    AnalysisReport,
    LoudnessPoint,
    ProgressCallback,
    ProgressEvent,
    Scene,
    SceneSemantics,
    Transcript,
    UsageTotals,
    compute_overall,
    noop_progress,
)

log = logging.getLogger(__name__)


def working_dir_for(asset_id: str) -> Path:
    base = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    d = base / "working" / asset_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "thumbs").mkdir(parents=True, exist_ok=True)
    return d


def _stamp_path(json_path: Path) -> Path:
    return json_path.with_suffix(json_path.suffix + ".stamp")


def _write_stamp(json_path: Path, payload: dict) -> None:
    _stamp_path(json_path).write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def _stamp_matches(json_path: Path, expected: dict) -> bool:
    p = _stamp_path(json_path)
    if not p.exists() or not json_path.exists():
        return False
    try:
        have = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return have == expected


def _source_mtime(source: Path) -> float:
    try:
        return source.stat().st_mtime
    except OSError:
        return 0.0


async def analyze(
    asset: MediaAsset,
    config: AnalysisConfig,
    progress: ProgressCallback = noop_progress,
) -> AnalysisReport:
    init_db()
    wd = working_dir_for(asset.id)
    t_start = time.monotonic()

    await progress(ProgressEvent("probe", 0.0, compute_overall("probe", 0.0)))
    write_json_atomic(wd / "probe.json", asset_to_dict(asset))
    await progress(ProgressEvent("probe", 1.0, compute_overall("probe", 1.0)))

    mtime = _source_mtime(asset.path)

    # --- scenes ----------------------------------------------------------------
    scenes_path = wd / "scenes.json"
    scenes_stamp = {
        "threshold": config.scene_threshold,
        "min_duration": config.min_scene_duration,
        "source_mtime": mtime,
    }
    if config.resume and _stamp_matches(scenes_path, scenes_stamp):
        scenes = [Scene.model_validate(s) for s in json.loads(scenes_path.read_text())]
        await progress(ProgressEvent("scenes", 1.0, compute_overall("scenes", 1.0)))
        log.info("scenes: resume cache hit (%d scenes)", len(scenes))
    else:
        try:
            scenes = await detect_scenes(asset, wd, config, progress)
        except AnalysisError:
            raise
        except Exception as exc:
            from reelforge_core.errors import SceneDetectionError

            raise SceneDetectionError(str(exc)) from exc
        _write_stamp(scenes_path, scenes_stamp)

    # --- transcribe ------------------------------------------------------------
    transcript_path = wd / "transcript.json"
    transcript_stamp = {
        "model": config.whisper_model,
        "source_mtime": mtime,
    }
    transcript: Transcript | None
    if config.resume and _stamp_matches(transcript_path, transcript_stamp):
        raw = json.loads(transcript_path.read_text())
        if raw.get("transcript") is None:
            transcript = None
        else:
            transcript = Transcript.model_validate(raw)
        await progress(ProgressEvent("transcribe", 1.0, compute_overall("transcribe", 1.0)))
        log.info("transcribe: resume cache hit")
    else:
        transcript = await transcribe(asset, wd, config, progress)
        _write_stamp(transcript_path, transcript_stamp)

    # --- loudness --------------------------------------------------------------
    loudness_path = wd / "loudness.json"
    loudness_stamp = {"source_mtime": mtime}
    loudness: list[LoudnessPoint]
    if config.resume and _stamp_matches(loudness_path, loudness_stamp):
        loudness = [
            LoudnessPoint.model_validate(p) for p in json.loads(loudness_path.read_text())
        ]
        await progress(ProgressEvent("loudness", 1.0, compute_overall("loudness", 1.0)))
        log.info("loudness: resume cache hit (%d points)", len(loudness))
    else:
        loudness = await measure_loudness(asset, wd, config, progress)
        _write_stamp(loudness_path, loudness_stamp)

    # --- semantics -------------------------------------------------------------
    # Semantics uses its own SQLite cache; no stamp file needed.
    semantics: list[SceneSemantics]
    usage: UsageTotals
    semantics, usage = await analyze_semantics(
        asset, scenes, transcript, wd, config, progress
    )

    elapsed = time.monotonic() - t_start
    report = AnalysisReport(
        asset_id=asset.id,
        source_path=str(asset.path),
        duration=asset.probe.duration_s,
        width=asset.probe.width,
        height=asset.probe.height,
        fps=asset.fps,
        has_audio=asset.has_audio,
        config=config,
        scenes=scenes,
        transcript=transcript,
        loudness=loudness,
        semantics=semantics,
        created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=round(elapsed, 3),
        reelforge_version=REELFORGE_VERSION,
        anthropic_usage=usage.model_dump(),
    )

    write_json_atomic(wd / "analysis.json", json.loads(report.model_dump_json()))
    return report
