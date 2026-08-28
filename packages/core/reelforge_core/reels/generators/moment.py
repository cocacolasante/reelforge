"""Moment-anchored candidate generator (pure).

Action footage without speech or hard cuts (GoPro runs, sports, b-roll) gives
the scene and sentence generators nothing to align to — but the *events* worth
reeling show up as spikes in the per-second energy track (motion + loudness
delta, analysis/energy.py). This generator finds those peaks and proposes
windows that place each peak early-ish in the reel, snapping edges to natural
seams (scene cuts, quiet audio) where any exist nearby.
"""

from __future__ import annotations

from reelforge_core.models import AnalysisReport, ReelCandidate, SelectionConfig

MOTION_WEIGHT = 0.6
LOUDNESS_WEIGHT = 0.4
PEAK_MIN_SEPARATION_SEC = 8.0
MAX_PEAKS = 25
# Where the peak lands inside the proposed reel (fraction of its duration).
PEAK_POSITIONS = (0.15, 0.35, 0.55)
EDGE_SNAP_WINDOW_SEC = 2.0


def combined_scores(analysis: AnalysisReport) -> list[tuple[float, float]]:
    """(time_sec, combined z-score) per energy bin. Pure."""
    energy = analysis.energy
    if not energy:
        return []
    motion = [p.motion for p in energy]
    deltas = [p.loudness_delta for p in energy]
    zm = _z_scores(motion)
    zd = _z_scores(deltas)
    return [
        (p.time_sec, MOTION_WEIGHT * m + LOUDNESS_WEIGHT * d)
        for p, m, d in zip(energy, zm, zd)
    ]


def _z_scores(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = var**0.5
    if std < 1e-9:
        return [0.0] * n
    return [(v - mean) / std for v in values]


def pick_peaks(
    scored: list[tuple[float, float]],
    min_separation: float = PEAK_MIN_SEPARATION_SEC,
    max_peaks: int = MAX_PEAKS,
) -> list[float]:
    """Times of local maxima, strongest first, greedily enforcing separation."""
    n = len(scored)
    maxima: list[tuple[float, float]] = []  # (score, time)
    for i in range(n):
        s = scored[i][1]
        # Endpoints must strictly beat their one neighbor — otherwise the
        # first bin of any flat run counts as a "peak".
        if i == 0:
            is_peak = n > 1 and s > scored[1][1]
        elif i == n - 1:
            is_peak = s > scored[i - 1][1]
        else:
            is_peak = s > scored[i - 1][1] and s >= scored[i + 1][1]
        if is_peak:
            maxima.append((s, scored[i][0]))
    maxima.sort(key=lambda p: (-p[0], p[1]))
    kept: list[float] = []
    for _, t in maxima:
        if len(kept) >= max_peaks:
            break
        if all(abs(t - k) >= min_separation for k in kept):
            kept.append(t)
    return kept


def _snap_edge(
    t: float,
    scene_cuts: list[float],
    analysis: AnalysisReport,
    window: float = EDGE_SNAP_WINDOW_SEC,
) -> float:
    """Snap to the nearest scene cut within ±window, else the quietest
    loudness bin within ±window, else leave the edge where it is."""
    near_cuts = [c for c in scene_cuts if abs(c - t) <= window]
    if near_cuts:
        return min(near_cuts, key=lambda c: abs(c - t))
    near_bins = [p for p in analysis.loudness if abs(p.time_sec - t) <= window]
    if near_bins:
        quietest = min(near_bins, key=lambda p: (p.lufs, abs(p.time_sec - t)))
        return quietest.time_sec
    return t


def generate_moment_candidates(
    analysis: AnalysisReport, config: SelectionConfig
) -> list[ReelCandidate]:
    from reelforge_core.reels.candidates import _candidate_id, covering_scenes

    scored = combined_scores(analysis)
    # A flat track (constant footage, failed decode -> zeros) has no signal;
    # don't fabricate peaks from it.
    if not scored or all(abs(s) < 1e-12 for _, s in scored):
        return []
    duration = analysis.duration
    min_sec = config.effective_min_sec
    max_sec = config.effective_max_sec
    if duration < min_sec:
        return []
    durations = sorted({min_sec, (min_sec + max_sec) / 2.0, max_sec})
    scene_cuts = sorted(
        {s.start_sec for s in analysis.scenes}
        | ({analysis.scenes[-1].end_sec} if analysis.scenes else set())
    )

    out: list[ReelCandidate] = []
    seen: set[tuple[int, int]] = set()
    for peak_t in pick_peaks(scored):
        for frac in PEAK_POSITIONS:
            for dur in durations:
                start = peak_t - frac * dur
                end = start + dur
                # Clamp the window into the asset, preserving duration.
                if start < 0.0:
                    start, end = 0.0, min(dur, duration)
                if end > duration:
                    end = duration
                    start = max(0.0, end - dur)
                # Snap each edge to a natural seam, but never let a snap push
                # the duration outside the target window.
                snapped = _snap_edge(start, scene_cuts, analysis)
                if 0.0 <= snapped < end and min_sec <= end - snapped <= max_sec:
                    start = snapped
                snapped = _snap_edge(end, scene_cuts, analysis)
                if start < snapped <= duration and min_sec <= snapped - start <= max_sec:
                    end = snapped
                final_dur = end - start
                if not (min_sec <= final_dur <= max_sec):
                    continue
                key = (int(round(start * 1000)), int(round(end * 1000)))
                if key in seen:
                    continue
                seen.add(key)
                covered = covering_scenes(analysis.scenes, start, end)
                out.append(
                    ReelCandidate(
                        candidate_id=_candidate_id(analysis.asset_id, start, end),
                        scene_indices=covered,
                        start_sec=start,
                        end_sec=end,
                        duration_sec=round(final_dur, 6),
                        scene_count=len(covered),
                        source="moment",
                    )
                )
    return out
