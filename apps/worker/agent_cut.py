"""One agent request -> finished clips.

An agent asks once and polls once; it should not have to drive analyze,
then select, then compose, then export, each with its own job id. This
module sequences the existing stages for a whole project, exactly as
`apps/queue_consumer/handler.py` does for growth-agent. Nothing in
`reelforge_core/reels/` is touched or reimplemented — this only calls it
and reads the artifacts it leaves behind.

Analysis is reused when `analysis.json` already exists: it is the most
expensive stage and is keyed by the asset's content hash, so a second cut
of the same footage pays only for what changed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from reelforge_core.analysis import analyze
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.compose import compose
from reelforge_core.export import export
from reelforge_core.ingest import MediaAsset, probe
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    ComposeConfig,
    ProgressEvent,
    RankedReel,
    SelectionConfig,
)
from reelforge_core.reels import select_reels

from apps.worker.delivery import deliver

log = logging.getLogger(__name__)

SOCIAL_PRESET = "mp4_h264_social"
# Stage weights for one overall progress bar. Analysis dominates a first
# run; on a re-cut it is skipped and the bar simply moves faster.
W_ANALYZE = 0.35
W_SELECT = 0.15
W_RENDER = 0.50

ProgressFn = Callable[[ProgressEvent], Awaitable[None]]


@dataclass
class CutRequest:
    project_id: str
    sources: list[tuple[str, str, str]]  # (asset_id, path, filename)
    top_k: int = 3
    min_sec: float | None = None
    max_sec: float | None = None
    prompt: str | None = None
    aspect: str = "9:16"
    # Which delivery routes to run once the clips exist.
    delivery: list[str] = field(default_factory=lambda: ["links"])
    project_name: str = "your footage"


async def _emit(progress: ProgressFn, stage: str, overall: float, message: str) -> None:
    await progress(
        ProgressEvent(  # type: ignore[arg-type]
            stage=stage,
            stage_progress=0.0,
            overall_progress=max(0.0, min(1.0, overall)),
            message=message,
        )
    )


def _load_asset(asset_id: str, path: str) -> MediaAsset:
    """Probe from disk. The id is content-derived, so this returns the same
    asset id the upload minted — no DB read needed in the worker."""
    return probe(Path(path))


async def ensure_analyses(
    sources: list[tuple[str, str, str]], progress: ProgressFn
) -> dict[str, tuple[MediaAsset, AnalysisReport]]:
    """Analyze anything not analyzed yet. Unreadable clips are skipped with a
    warning rather than failing the whole request — one bad file in a batch
    of six should still yield five clips' worth of footage."""
    out: dict[str, tuple[MediaAsset, AnalysisReport]] = {}
    total = max(1, len(sources))
    for i, (asset_id, path, filename) in enumerate(sources):
        share = W_ANALYZE * (i / total)
        await _emit(progress, "analyze", share, f"watching {filename} ({i + 1}/{total})")
        try:
            asset = _load_asset(asset_id, path)
            analysis_path = working_dir_for(asset.id) / "analysis.json"
            if analysis_path.exists():
                report = AnalysisReport.model_validate_json(analysis_path.read_text())
                log.info("agent cut: reusing analysis for %s", asset.id[:12])
            else:
                report = await analyze(asset, AnalysisConfig())
            out[asset.id] = (asset, report)
        except Exception as exc:  # noqa: BLE001 — one clip must not sink the batch
            log.exception("agent cut: could not analyze %s", filename)
            _ = exc
    return out


def _selection_config(req: CutRequest) -> SelectionConfig:
    overrides: dict = {"top_k": req.top_k}
    if req.min_sec is not None:
        overrides["target_min_sec"] = req.min_sec
    if req.max_sec is not None:
        overrides["target_max_sec"] = req.max_sec
    if req.prompt:
        overrides["prompt"] = req.prompt
    return SelectionConfig(**overrides)


async def cut_reels(req: CutRequest, progress: ProgressFn) -> dict:
    """Analyze -> select -> compose + export the best clips of a project."""
    started = time.monotonic()
    analyses = await ensure_analyses(req.sources, progress)
    if not analyses:
        raise RuntimeError(
            "none of this footage could be read — check the clips uploaded cleanly"
        )

    # Selection runs per clip (it ranks spans within one asset), then the
    # best across the whole batch are what actually get rendered.
    ranked: list[tuple[MediaAsset, AnalysisReport, RankedReel]] = []
    for i, (asset_id, (asset, report)) in enumerate(analyses.items()):
        overall = W_ANALYZE + W_SELECT * (i / max(1, len(analyses)))
        await _emit(progress, "select", overall, f"picking moments ({i + 1}/{len(analyses)})")
        try:
            selection = await select_reels(report, _selection_config(req))
        except Exception:
            log.exception("agent cut: selection failed for %s", asset_id[:12])
            continue
        for reel in selection.reels:
            ranked.append((asset, report, reel))

    if not ranked:
        note = (
            "nothing matched that direction — try broader wording"
            if req.prompt
            else "no clips worth cutting were found in this footage"
        )
        return {
            "projectId": req.project_id,
            "clips": [],
            "note": note,
            "elapsedSec": round(time.monotonic() - started, 1),
        }

    ranked.sort(key=lambda r: r[2].overall, reverse=True)
    chosen = ranked[: req.top_k]

    clips: list[dict] = []
    failures: list[dict] = []
    for i, (asset, report, reel) in enumerate(chosen):
        overall = W_ANALYZE + W_SELECT + W_RENDER * (i / max(1, len(chosen)))
        await _emit(
            progress, "render", overall, f"rendering {reel.title!r} ({i + 1}/{len(chosen)})"
        )
        try:
            await compose(asset, reel, report, ComposeConfig(aspect=req.aspect))
            exported = await export(asset.id, reel.candidate_id, SOCIAL_PRESET)
            path = Path(exported.output_path)
            if not path.exists():
                raise FileNotFoundError(f"export reported {path} but it is not on disk")
        except Exception as exc:  # noqa: BLE001 — one clip failing keeps the rest
            log.exception("agent cut: render failed for %s", reel.candidate_id)
            failures.append({"title": reel.title, "error": str(exc)[:300]})
            continue
        clips.append(
            {
                "clipId": reel.candidate_id,
                "assetId": asset.id,
                "title": reel.title,
                "hook": reel.hook,
                "durationSec": round(reel.duration_sec, 1),
                "startSec": round(reel.start_sec, 1),
                "score": round(reel.overall, 1),
                "mood": reel.suggested_mood,
                "exportPath": str(path),
                "bytes": path.stat().st_size,
            }
        )

    await _emit(progress, "render", 1.0, f"{len(clips)} clip(s) ready")
    result: dict = {
        "projectId": req.project_id,
        "clips": clips,
        "elapsedSec": round(time.monotonic() - started, 1),
    }
    if failures:
        result["failures"] = failures
    result["delivery"] = deliver(clips, req.delivery, req.project_name)
    return result


async def export_and_deliver_long(
    reel_id: str, asset_id: str, req: CutRequest, mix_result: dict
) -> dict:
    """A long video renders a mezzanine but never an export, so there is no
    file to hand over until now."""
    from reelforge_core.export import export as export_fn

    exported = await export_fn(asset_id, reel_id, SOCIAL_PRESET)
    path = Path(exported.output_path)
    clip = {
        "clipId": reel_id,
        "assetId": asset_id,
        "title": mix_result.get("title") or "Long video",
        "durationSec": round(float(mix_result.get("duration_sec") or 0.0), 1),
        "exportPath": str(path),
        "bytes": path.stat().st_size if path.exists() else 0,
    }
    return {
        "projectId": req.project_id,
        "clips": [clip],
        "longVideo": True,
        "delivery": deliver([clip], req.delivery, req.project_name),
    }
