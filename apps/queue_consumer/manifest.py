"""Assemble the manifest by merging per-stage artifacts.

Every stage of the selection pipeline already writes its own scores to
`/data/working/{asset_id}/`:

    prescore.json   heuristic pre-score + the linear formula's feature inputs
    reels.json      the listwise ReelScores, overall, rank_position, and the
                    pre-refinement bounds that boundary adjustment recorded

This module joins them on `candidate_id` rather than recomputing anything. That
matters: the manifest must describe the decision the pipeline actually made, not
a re-derivation that could drift from it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from reelforge_core.models import RankedReel, ReelSelection

from apps.queue_consumer.contract import (
    BoundaryStage,
    ManifestClip,
    PrescoreStage,
    RankStage,
    SelectionProvenance,
    StageScores,
)

log = logging.getLogger(__name__)


def _load_prescores(working_dir: Path) -> tuple[dict[str, dict], str | None]:
    """candidate_id -> its prescore record. Absent on very old working dirs."""
    path = working_dir / "prescore.json"
    if not path.exists():
        log.warning("no prescore.json in %s; manifest will omit that stage", working_dir)
        return {}, None
    try:
        rows = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}, None

    version = None
    try:
        from reelforge_core.reels.prescore import PRESCORE_VERSION

        version = PRESCORE_VERSION
    except Exception:  # noqa: BLE001 - version is provenance, not correctness
        pass
    return {row["candidate_id"]: row for row in rows if "candidate_id" in row}, version


def _boundary_stage(reel: RankedReel) -> BoundaryStage:
    """
    `pre_refine_*` is set only when refinement actually moved an edge, so its
    absence is the signal that the boundary is the candidate's original one.
    """
    pre_start = reel.pre_refine_start_sec
    pre_end = reel.pre_refine_end_sec
    if pre_start is None and pre_end is None:
        return BoundaryStage(adjusted=False)
    return BoundaryStage(
        adjusted=True,
        pre_refine_start_sec=pre_start,
        pre_refine_end_sec=pre_end,
        start_delta_sec=None if pre_start is None else round(reel.start_sec - pre_start, 3),
        end_delta_sec=None if pre_end is None else round(reel.end_sec - pre_end, 3),
    )


def _rank_stage(reel: RankedReel) -> RankStage:
    return RankStage(
        narrative_coherence=reel.scores.narrative_coherence,
        hook_strength=reel.scores.hook_strength,
        emotional_payoff=reel.scores.emotional_payoff,
        standalone_clarity=reel.scores.standalone_clarity,
        weighted=round(reel.scores.weighted, 4),
        overall=round(reel.overall, 4),
        rank_position=reel.rank_position,
        prompt_relevance=reel.prompt_relevance,
        source=reel.source,
        edit_style=reel.edit_style,
    )


def build_clips(selection: ReelSelection, working_dir: Path) -> list[ManifestClip]:
    prescores, prescore_version = _load_prescores(working_dir)

    clips: list[ManifestClip] = []
    for reel in selection.reels:
        row = prescores.get(reel.candidate_id)
        prescore = (
            PrescoreStage(
                value=row.get("prescore", 0.0),
                version=prescore_version or "unknown",
                shortlisted=bool(row.get("shortlisted", True)),
                features=row.get("features", {}),
            )
            if row
            else None
        )

        clips.append(
            ManifestClip(
                reelforge_clip_id=reel.candidate_id,
                start_sec=reel.start_sec,
                end_sec=reel.end_sec,
                duration_sec=reel.duration_sec,
                rank=reel.rank,
                title=reel.title,
                hook=reel.hook,
                stage_scores=StageScores(
                    prescore=prescore,
                    rank=_rank_stage(reel),
                    boundary=_boundary_stage(reel),
                ),
            )
        )
    return clips


def build_provenance(selection: ReelSelection, working_dir: Path) -> SelectionProvenance:
    _, version = _load_prescores(working_dir)
    return SelectionProvenance(
        candidates_generated=selection.candidates_generated,
        dropped_by_dedup=selection.candidates_dropped_by_dedup,
        dropped_by_diversity=selection.candidates_dropped_by_diversity,
        prescore_version=version,
    )
