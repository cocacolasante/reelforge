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
    from reelforge_core.reels.prescore import PRESCORE_VERSION

    stamp = {
        "ranking_model": config.ranking_model,
        "ranking_prompt_version": config.ranking_prompt_version,
        "temperature": config.temperature,
        # Hash of the SHORTLIST (not the full union) — a prescore change that
        # alters shortlist membership invalidates resume via the hash; a
        # weights change that happens NOT to alter membership still must
        # invalidate (features ride into the v2 ranking context), hence the
        # explicit version key.
        "candidate_hash": cand_hash,
        "prescore_version": PRESCORE_VERSION,
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
# Boundary refinement (stamped, resumable — the pure logic lives in
# reels/refine.py)
# ---------------------------------------------------------------------------


def _refine_stamp(config: SelectionConfig, reels: list[RankedReel]) -> dict:
    from reelforge_core.reels.refine import REFINE_PROMPT_VERSION

    targets = sorted(
        f"{r.candidate_id}:{int(round(r.start_sec * 1000))}:{int(round(r.end_sec * 1000))}"
        for r in reels
    )
    return {
        "model": config.ranking_model,
        "refine_prompt_version": REFINE_PROMPT_VERSION,
        "targets": "|".join(targets),
    }


async def _refine_step(
    final: list[RankedReel],
    analysis: AnalysisReport,
    config: SelectionConfig,
    wd: Path,
) -> tuple[list[RankedReel], UsageTotals]:
    from reelforge_core.reels.refine import apply_refinements_raw, refine_reels

    raw_path = wd / "refine_raw.json"
    stamp_path = wd / "refine_raw.json.stamp"
    stamp = _refine_stamp(config, final)
    if config.resume and raw_path.exists() and _stamp_matches(stamp_path, stamp):
        log.info("refinement: resume cache hit (%d reels)", len(final))
        raw = json.loads(raw_path.read_text()).get("refinements", [])
        return apply_refinements_raw(final, raw, analysis, config), UsageTotals()

    refined, usage, raw = await refine_reels(final, analysis, config)
    if raw:  # persist only successful calls so a failure retries next run
        write_json_atomic(
            raw_path, {"refinements": raw, "usage": usage.model_dump()}
        )
        _write_stamp(stamp_path, stamp)
    return refined, usage


# ---------------------------------------------------------------------------
# Contact-sheet extraction (I/O — the pure command builder lives in
# reels/contact_sheet.py)
# ---------------------------------------------------------------------------


async def _extract_contact_sheets(
    candidates: list[ReelCandidate],
    analysis: AnalysisReport,
    features: dict,
    wd: Path,
) -> dict[str, Path]:
    """candidate_id -> sheet path for every candidate whose sheet could be
    produced. Best-effort: a missing source or a failed extraction just means
    that candidate is ranked text-only."""
    import asyncio

    from reelforge_core.compose.graph import run_ffmpeg
    from reelforge_core.reels.contact_sheet import (
        build_contact_sheet_command,
        sheet_frame_times,
    )

    source = Path(analysis.source_path)
    if not source.exists():
        log.warning("contact sheets skipped: source missing at %s", source)
        return {}
    sheets_dir = wd / "candidates"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(4)

    async def _one(c: ReelCandidate) -> tuple[str, Path] | None:
        out = sheets_dir / f"{c.candidate_id}.jpg"
        if out.exists():
            return c.candidate_id, out
        f = features.get(c.candidate_id)
        times = sheet_frame_times(
            c.start_sec, c.end_sec, getattr(f, "energy_peak_pos", None)
        )
        cmd = build_contact_sheet_command(source, times, out)
        try:
            async with sem:
                await asyncio.to_thread(run_ffmpeg, cmd, timeout_sec=120)
        except Exception as exc:
            log.warning("contact sheet failed for %s: %s", c.candidate_id, exc)
            return None
        return c.candidate_id, out

    results = await asyncio.gather(*(_one(c) for c in candidates))
    return dict(r for r in results if r is not None)


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

    # ----- prescore + shortlist (local, no API) -----
    from reelforge_core.reels.prescore import compute_features, prescore, shortlist

    features = compute_features(candidates, analysis)
    short = shortlist(candidates, features, config.shortlist_size)
    short_ids = {s.candidate_id for s in short}
    write_json_atomic(
        wd / "prescore.json",
        sorted(
            (
                {
                    "candidate_id": c.candidate_id,
                    "start_sec": c.start_sec,
                    "end_sec": c.end_sec,
                    "source": c.source,
                    "prescore": prescore(features[c.candidate_id]),
                    "shortlisted": c.candidate_id in short_ids,
                    "features": features[c.candidate_id].to_dict(),
                }
                for c in candidates
            ),
            key=lambda row: -row["prescore"],
        ),
    )
    log.info(
        "prescore: shortlisted %d of %d candidates", len(short), len(candidates)
    )

    # ----- ranking (with stamp-based resume) -----
    cand_hash = candidate_set_hash(short)
    stamp_path = wd / "ranking_raw.json.stamp"
    raw_path = wd / "ranking_raw.json"
    stamp = _ranking_stamp(config, cand_hash)

    sheets: dict[str, Path] = {}
    ranking_result: RankingResult | None = None
    if config.resume and raw_path.exists() and _stamp_matches(stamp_path, stamp):
        log.info("ranking: resume cache hit (%d candidates)", len(short))
        raw = json.loads(raw_path.read_text())
        rankings_raw = raw.get("rankings", [])
        candidate_map = {c.candidate_id: c for c in short}
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
        await progress(_emit("ranking", 0.02, "extracting contact sheets"))
        sheets = await _extract_contact_sheets(short, analysis, features, wd)
        await progress(_emit("ranking", 0.05, f"ranking {len(short)} candidates"))
        ranking_result = await rank(
            short, analysis, config, features=features, sheets=sheets
        )
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

    # ----- MMR diversity re-rank (halved λ under a user prompt) -----
    from reelforge_core.reels.dedup import mmr_diversify, resolve_post_refine_overlaps

    lam = config.diversity_lambda * (0.5 if config.prompt else 1.0)
    sem_tags = {s.scene_index: set(s.tags) for s in analysis.semantics}
    tag_sets = {
        r.candidate_id: set().union(*(sem_tags.get(i, set()) for i in r.scene_indices))
        if r.scene_indices
        else set()
        for r in kept
    }
    ordered = mmr_diversify(kept, tag_sets, lam)
    topk_by_overall = {r.candidate_id for r in kept[: config.top_k]}
    topk_by_mmr = {r.candidate_id for r in ordered[: config.top_k]}
    dropped_by_diversity = len(topk_by_overall - topk_by_mmr)
    if dropped_by_diversity:
        log.info(
            "diversity: %d reel(s) displaced from the top-%d by MMR (λ=%.1f)",
            dropped_by_diversity,
            config.top_k,
            lam,
        )

    final = ordered[: config.top_k]
    reserve = ordered[config.top_k :]

    # ----- boundary refinement (best-effort, one small API call) -----
    refine_usage = UsageTotals()
    if config.refine and final:
        await progress(_emit("dedup", 0.5, "refining reel bounds"))
        final, refine_usage = await _refine_step(final, analysis, config, wd)
        # Refined edges can newly collide; drop the lower-ordered reel and
        # backfill from the post-MMR reserve.
        final = resolve_post_refine_overlaps(final, reserve, config)
    final = assign_ranks_and_truncate(final, config.top_k)
    await progress(_emit("dedup", 1.0))

    total_usage = UsageTotals(
        input_tokens=ranking_result.usage.input_tokens + refine_usage.input_tokens,
        output_tokens=ranking_result.usage.output_tokens + refine_usage.output_tokens,
        cache_hits=ranking_result.usage.cache_hits,
    )

    selection = ReelSelection(
        asset_id=analysis.asset_id,
        analysis_source="analysis.json",
        config=config,
        candidates_generated=len(candidates),
        candidates_dropped_by_dedup=dropped,
        candidates_dropped_by_diversity=dropped_by_diversity,
        reels=final,
        anthropic_usage=total_usage.model_dump(),
        created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_sec=round(time.monotonic() - t_start, 3),
        reelforge_version=REELFORGE_VERSION,
    )
    write_json_atomic(wd / "reels.json", json.loads(selection.model_dump_json()))
    return selection
