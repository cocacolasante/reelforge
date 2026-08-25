"""select_reels orchestrator: candidates → ranking (with stamp-based resume) → dedup."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal

from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    REELFORGE_VERSION,
    AnalysisReport,
    ProgressEvent,
    RankedReel,
    ReelCandidate,
    ReelScores,
    ReelSelection,
    SelectionConfig,
    UsageTotals,
)
from reelforge_core.reels.candidates import candidate_set_hash, generate_candidates
from reelforge_core.reels.dedup import assign_ranks_and_truncate, dedup
from reelforge_core.reels.rank import RankingResult, rank

log = logging.getLogger(__name__)

SelectionStage = Literal["candidates", "ranking", "dedup"]
STAGE_WEIGHTS: dict[SelectionStage, float] = {
    "candidates": 0.05,
    "ranking": 0.90,
    "dedup": 0.05,
}


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


async def _noop(_: ProgressEvent) -> None:
    return None


def _overall(stage: SelectionStage, stage_progress: float) -> float:
    total = 0.0
    for s, w in STAGE_WEIGHTS.items():
        if s == stage:
            total += w * max(0.0, min(1.0, stage_progress))
            break
        total += w
    return min(1.0, total)


def _emit(stage: SelectionStage, sp: float, message: str | None = None) -> ProgressEvent:
    # Reuse the Phase 1 ProgressEvent shape so Redis readers don't need to know the
    # difference. We cast the stage into the wider Stage Literal via a type: ignore.
    return ProgressEvent(stage=stage, stage_progress=sp, overall_progress=_overall(stage, sp), message=message)  # type: ignore[arg-type]


def _working_dir(analysis: AnalysisReport) -> Path:
    base = Path(os.environ.get("REELFORGE_DATA_DIR", "/data"))
    return base / "working" / analysis.asset_id


# ---------------------------------------------------------------------------
# Ranking stamp (partial resume)
# ---------------------------------------------------------------------------


def _ranking_stamp(config: SelectionConfig, cand_hash: str) -> dict:
    stamp = {
        "ranking_model": config.ranking_model,
        "ranking_prompt_version": config.ranking_prompt_version,
        "temperature": config.temperature,
        "candidate_hash": cand_hash,
    }
    # Conditional so existing no-prompt stamps keep matching; any prompt
    # add/change/remove mismatches and forces a fresh ranking call.
    if config.prompt:
        stamp["prompt"] = config.prompt
    return stamp


def _write_stamp(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _stamp_matches(path: Path, expected: dict) -> bool:
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")) == expected
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def select_reels(
    analysis: AnalysisReport,
    config: SelectionConfig,
    progress: ProgressCallback = _noop,
) -> ReelSelection:
    t_start = time.monotonic()
    wd = _working_dir(analysis)
    wd.mkdir(parents=True, exist_ok=True)

    # ----- candidates -----
    await progress(_emit("candidates", 0.0))
    candidates = generate_candidates(analysis, config)
    write_json_atomic(wd / "candidates.json", [c.model_dump() for c in candidates])
    await progress(_emit("candidates", 1.0))
    log.info(
        "selection: %d candidates from %d scenes",
        len(candidates),
        len(analysis.scenes),
    )

    if not candidates:
        selection = ReelSelection(
            asset_id=analysis.asset_id,
            analysis_source="analysis.json",
            config=config,
            candidates_generated=0,
            candidates_dropped_by_dedup=0,
            reels=[],
            anthropic_usage=UsageTotals().model_dump(),
            created_at=datetime.now(timezone.utc).isoformat(),
            elapsed_sec=round(time.monotonic() - t_start, 3),
            reelforge_version=REELFORGE_VERSION,
        )
        write_json_atomic(wd / "reels.json", json.loads(selection.model_dump_json()))
        await progress(_emit("ranking", 1.0))
        await progress(_emit("dedup", 1.0))
        return selection

    # ----- ranking (with stamp-based resume) -----
    cand_hash = candidate_set_hash(candidates)
    stamp_path = wd / "ranking_raw.json.stamp"
    raw_path = wd / "ranking_raw.json"
    stamp = _ranking_stamp(config, cand_hash)

    ranking_result: RankingResult | None = None
    if config.resume and raw_path.exists() and _stamp_matches(stamp_path, stamp):
        log.info("ranking: resume cache hit (%d candidates)", len(candidates))
        raw = json.loads(raw_path.read_text())
        rankings_raw = raw.get("rankings", [])
        candidate_map = {c.candidate_id: c for c in candidates}
        from reelforge_core.reels.rank import _coerce_rankings  # local import

        ranked = _coerce_rankings(
            rankings_raw,
            candidate_map=candidate_map,
            prompt_active=bool(config.prompt),
        )
        ranking_result = RankingResult(
            reels=ranked, usage=UsageTotals(), raw_rankings=rankings_raw
        )
        await progress(_emit("ranking", 1.0))
    else:
        await progress(_emit("ranking", 0.05, f"ranking {len(candidates)} candidates"))
        ranking_result = await rank(candidates, analysis, config)
        await progress(_emit("ranking", 1.0))
        # Persist raw rankings + stamp so re-runs can skip the Claude call.
        write_json_atomic(
            raw_path,
            {
                "rankings": ranking_result.raw_rankings,
                "usage": ranking_result.usage.model_dump(),
                "candidate_hash": cand_hash,
            },
        )
        _write_stamp(stamp_path, stamp)

    # ----- prompt-relevance gate (strict filter) + dedup + top-k -----
    await progress(_emit("dedup", 0.0))
    reels_for_dedup = ranking_result.reels
    if config.prompt:
        from reelforge_core.reels.rank import PROMPT_RELEVANCE_FLOOR  # local import

        before = len(reels_for_dedup)
        reels_for_dedup = [
            r
            for r in reels_for_dedup
            if (r.prompt_relevance or 0) >= PROMPT_RELEVANCE_FLOOR
        ]
        if before != len(reels_for_dedup):
            log.info(
                "prompt gate: %d/%d candidates below relevance floor %d dropped",
                before - len(reels_for_dedup),
                before,
                PROMPT_RELEVANCE_FLOOR,
            )
    kept, dropped = dedup(reels_for_dedup, config)
    final = assign_ranks_and_truncate(kept, config.top_k)
    await progress(_emit("dedup", 1.0))

    selection = ReelSelection(
        asset_id=analysis.asset_id,
        analysis_source="analysis.json",
        config=config,
        candidates_generated=len(candidates),
        candidates_dropped_by_dedup=dropped,
        reels=final,
        anthropic_usage=ranking_result.usage.model_dump(),
        created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=round(time.monotonic() - t_start, 3),
        reelforge_version=REELFORGE_VERSION,
    )
    write_json_atomic(wd / "reels.json", json.loads(selection.model_dump_json()))
    return selection
