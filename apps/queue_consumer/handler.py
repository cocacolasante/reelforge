"""Drive the existing pipeline for one queued job.

    signed URL -> /data/inbox -> probe -> analyze -> select -> compose/export -> manifest

Every step calls the same functions the CLI and the arq worker call. Nothing in
`reelforge_core/reels/` is touched, imported around, or re-implemented — this
module only sequences them and reads the artifacts they leave behind.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from reelforge_core.analysis import analyze
from reelforge_core.analysis.pipeline import working_dir_for
from reelforge_core.ingest import probe
from reelforge_core.models import AnalysisConfig, AnalysisReport, SelectionConfig
from reelforge_core.paths import INBOX_DIR
from reelforge_core.reels import select_reels

from apps.queue_consumer import storage
from apps.queue_consumer.contract import ClipJob, Manifest
from apps.queue_consumer.manifest import build_clips, build_provenance
from apps.queue_consumer.render import render_variants

log = logging.getLogger(__name__)


def _selection_config(job: ClipJob) -> SelectionConfig:
    """Overrides are optional; anything absent keeps the pipeline's own default."""
    overrides: dict = {}
    if job.selection:
        if job.selection.top_k is not None:
            overrides["top_k"] = job.selection.top_k
        if job.selection.min_sec is not None:
            overrides["target_min_sec"] = job.selection.min_sec
        if job.selection.max_sec is not None:
            overrides["target_max_sec"] = job.selection.max_sec
        if job.selection.prompt:
            overrides["prompt"] = job.selection.prompt
    return SelectionConfig(**overrides)


async def _reuse_or_build_analysis(asset, config: AnalysisConfig) -> AnalysisReport:
    """
    Analysis is by far the most expensive stage and is keyed by the asset's
    content hash, so an identical source re-submitted (a retry, a duplicate
    upload) reuses the existing report instead of paying for it twice.
    """
    analysis_path = working_dir_for(asset.id) / "analysis.json"
    if analysis_path.exists():
        try:
            report = AnalysisReport.model_validate_json(analysis_path.read_text())
            log.info("reusing existing analysis for asset %s", asset.id)
            return report
        except Exception as exc:  # noqa: BLE001 - a corrupt cache must not be fatal
            log.warning("existing analysis.json unusable (%s); re-analysing", exc)
    return await analyze(asset, config)


async def run_job(job: ClipJob) -> Manifest:
    """Always returns a Manifest. Failures are reported, never raised at the queue."""
    started = time.monotonic()

    def failed(reason: str) -> Manifest:
        log.error("job %s failed: %s", job.job_id, reason)
        return Manifest(
            job_id=job.job_id,
            tenant_id=job.tenant_id,
            source_video_id=job.source_video_id,
            status="failed",
            error=reason[:1000],
            elapsed_sec=round(time.monotonic() - started, 2),
        )

    # 1. Pull the source down.
    try:
        local = storage.download(job.source_url, INBOX_DIR / f"{job.job_id}.mp4")
    except storage.StorageError as exc:
        return failed(str(exc))

    # 2. Probe — this is what mints the content-hash asset id.
    try:
        asset = probe(local)
    except Exception as exc:  # noqa: BLE001
        return failed(f"could not probe source video: {exc}")

    log.info("job %s -> asset %s (%.1fs)", job.job_id, asset.id, asset.probe.duration_s)

    # 3+4. Analyze, then select. Both unmodified.
    try:
        analysis = await _reuse_or_build_analysis(asset, AnalysisConfig())
    except Exception as exc:  # noqa: BLE001
        return failed(f"analysis failed: {exc}")

    try:
        selection = await select_reels(analysis, _selection_config(job))
    except Exception as exc:  # noqa: BLE001
        return failed(f"selection failed: {exc}")

    working_dir: Path = working_dir_for(asset.id)
    clips = build_clips(selection, working_dir)

    # 5. Render per-platform variants. A failure here degrades the clip rather
    # than the job: the selection decision and its scores are still worth
    # returning, and re-rendering is cheap next to re-running selection.
    if job.platform_targets:
        by_id = {reel.candidate_id: reel for reel in selection.reels}
        for clip in clips:
            reel = by_id.get(clip.reelforge_clip_id)
            if reel is None:
                continue
            variants, errors = await render_variants(
                asset, reel, analysis, job.platform_targets, job.output_prefix
            )
            clip.variants = variants
            clip.variant_errors = errors

    return Manifest(
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        source_video_id=job.source_video_id,
        status="completed",
        asset_id=asset.id,
        reelforge_version=selection.reelforge_version,
        elapsed_sec=round(time.monotonic() - started, 2),
        selection=build_provenance(selection, working_dir),
        clips=clips,
    )
