"""Render per-platform variants for a selected clip.

Selection is platform-agnostic: it decides *which* span is worth cutting. This
module turns one such span into the concrete files each platform will accept, by
running the existing compose -> export pipeline once per distinct aspect ratio.

Two deliberate behaviours:

  * Targets are grouped by aspect. Three platforms that all want 9:16 cost one
    compose, not three — compose is the expensive step by an order of magnitude.
  * A clip longer than a platform's ceiling is REJECTED for that platform with a
    reason, not silently trimmed. Trimming would move the boundary the selection
    pipeline deliberately chose, and would make the performance labels in Phase 7
    describe a clip that was never actually ranked.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reelforge_core.compose import compose
from reelforge_core.export import export
from reelforge_core.ingest import MediaAsset
from reelforge_core.models import AnalysisReport, ComposeConfig, RankedReel

from apps.queue_consumer import storage
from apps.queue_consumer.contract import PlatformTarget, Variant, VariantError

log = logging.getLogger(__name__)

# h.264 in mp4 — the only preset every one of these platforms ingests without
# re-encoding surprises.
SOCIAL_PRESET = "mp4_h264_social"


async def render_variants(
    asset: MediaAsset,
    reel: RankedReel,
    analysis: AnalysisReport,
    targets: list[PlatformTarget],
    output_prefix: str,
) -> tuple[list[Variant], list[VariantError]]:
    variants: list[Variant] = []
    errors: list[VariantError] = []

    eligible: list[PlatformTarget] = []
    for target in targets:
        if reel.duration_sec > target.max_duration:
            errors.append(
                VariantError(
                    platform=target.platform,
                    error=(
                        f"clip is {reel.duration_sec:.1f}s but {target.platform} accepts at most "
                        f"{target.max_duration:.0f}s; not trimming, because that would move a "
                        f"boundary the selection pipeline chose"
                    ),
                )
            )
        else:
            eligible.append(target)

    # One compose+export per distinct aspect, shared by every platform wanting it.
    by_aspect: dict[str, list[PlatformTarget]] = {}
    for target in eligible:
        by_aspect.setdefault(target.aspect, []).append(target)

    for aspect, group in by_aspect.items():
        try:
            await compose(asset, reel, analysis, ComposeConfig(aspect=aspect))
            exported = await export(asset.id, reel.candidate_id, SOCIAL_PRESET)
            local = Path(exported.output_path)
            if not local.exists():
                raise FileNotFoundError(f"export reported {local} but it is not on disk")
        except Exception as exc:  # noqa: BLE001 - one aspect failing must not sink the clip
            log.exception("render failed for %s aspect %s", reel.candidate_id, aspect)
            for target in group:
                errors.append(VariantError(platform=target.platform, error=str(exc)[:400]))
            continue

        for target in group:
            key = f"{output_prefix.rstrip('/')}/{reel.candidate_id}/{target.platform}.mp4"
            try:
                size = storage.upload(local, key)
            except storage.StorageError as exc:
                errors.append(VariantError(platform=target.platform, error=str(exc)[:400]))
                continue
            variants.append(
                Variant(
                    platform=target.platform,
                    aspect=aspect,
                    duration_sec=reel.duration_sec,
                    key=key,
                    bytes=size,
                )
            )

    return variants, errors
