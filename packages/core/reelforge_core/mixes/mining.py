"""Moment mining + cross-clip pooling (pure, no API).

The mix's raw ingredients are SHORT highlight moments (a jump, a sentence,
a scene beat) mined from every analyzed video in the project — freshly, not
sliced from each clip's standalone reels. The existing selection machinery
does all the work at shorter duration bounds: candidate generators
(sentence/scene/moment), prescore features, and the overlap-dedupe walk.

Pooling is balanced round-robin by prescore rank across assets so one long
clip can't monopolize the sequencing call's attention.
"""

from __future__ import annotations

from dataclasses import dataclass

from reelforge_core.models import AnalysisReport, ReelCandidate, SelectionConfig
from reelforge_core.reels.prescore import (
    PrescoreFeatures,
    _time_overlap,
    compute_features,
    prescore,
)

# Moment duration bounds scale with the mix target so a 5-minute mix stays
# within the editor's 60-shot cap (and its shots don't feel like confetti).
SHORT_MIX_BOUNDS = (2.0, 8.0)  # target <= LONG_MIX_THRESHOLD_SEC
LONG_MIX_BOUNDS = (4.0, 12.0)
LONG_MIX_THRESHOLD_SEC = 90.0

# Cap on candidates mined per asset before pooling (keeps enumeration and
# feature computation bounded on fast-cut footage).
PER_ASSET_MINE_CAP = 120
# The sequencing call sees at most this many pooled moments.
POOL_CAP = 60
# Near-duplicate windows within one asset: same rule as the selection
# shortlist (intersection / shorter duration).
DEDUPE_OVERLAP = 0.85


@dataclass(frozen=True)
class MinedMoment:
    asset_id: str
    candidate: ReelCandidate
    features: PrescoreFeatures
    score: float

    @property
    def moment_id(self) -> str:
        # candidate_id hashes (asset_id | start_ms | end_ms) — globally unique.
        return self.candidate.candidate_id


def moment_bounds_for(target_duration_sec: float) -> tuple[float, float]:
    """Ingredient duration window for a given mix length. Pure."""
    if target_duration_sec > LONG_MIX_THRESHOLD_SEC:
        return LONG_MIX_BOUNDS
    return SHORT_MIX_BOUNDS


def mine_moments(
    analysis: AnalysisReport, bounds: tuple[float, float]
) -> list[MinedMoment]:
    """Short-span candidates for one asset, scored and dedeuped, best first."""
    from reelforge_core.reels import generate_candidates

    cfg = SelectionConfig(
        target_min_sec=bounds[0],
        target_max_sec=bounds[1],
        max_candidates=PER_ASSET_MINE_CAP,
    )
    candidates = generate_candidates(analysis, cfg)
    if not candidates:
        return []
    features = compute_features(candidates, analysis)
    moments = [
        MinedMoment(
            asset_id=analysis.asset_id,
            candidate=c,
            features=features[c.candidate_id],
            score=prescore(features[c.candidate_id]),
        )
        for c in candidates
    ]
    moments.sort(key=lambda m: (-m.score, m.candidate.duration_sec, m.candidate.start_sec))
    # Overlap-dedupe walk (same rule as the selection shortlist).
    kept: list[MinedMoment] = []
    for m in moments:
        if any(
            _time_overlap(m.candidate, k.candidate) > DEDUPE_OVERLAP for k in kept
        ):
            continue
        kept.append(m)
    return kept


def pool_moments(
    per_asset: dict[str, list[MinedMoment]], cap: int = POOL_CAP
) -> list[MinedMoment]:
    """Balanced round-robin merge across assets, each asset's list assumed
    best-first (mine_moments output). Deterministic: assets iterate in
    sorted-id order. Pure."""
    queues = {aid: list(ms) for aid, ms in sorted(per_asset.items()) if ms}
    pool: list[MinedMoment] = []
    while queues and len(pool) < cap:
        for aid in sorted(queues):
            if len(pool) >= cap:
                break
            queue = queues[aid]
            pool.append(queue.pop(0))
            if not queue:
                del queues[aid]
    return pool
