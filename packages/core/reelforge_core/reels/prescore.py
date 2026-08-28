"""Heuristic pre-score + shortlist (pure, no API).

Generators can propose hundreds of candidates; the ranker should only see the
plausible ones. Every feature here is computable locally; the linear formula
is deliberately simple and documented so it can be tuned against
`./reelforge eval-select`.

Formula (PRESCORE_VERSION bumps whenever weights change — it is part of the
ranking resume stamp):

    +25  starts_on_unit_boundary      (opens on a natural speech seam)
    +15  ends_on_unit_boundary
    -40  starts_mid_word              (opens mid-word — the cardinal sin)
    -25  ends_mid_word
    +10 * min(energy_peak_z, 3)       (contains a real event)
    +15  if energy_peak_pos < 0.2     (the event lands early — hook material)
    + 5 * min(n_scene_cuts, 4)        (visual variety)
    +10  if speech_ratio > 0.5        (substantial spoken content)

Ties break toward shorter duration (cheaper to fill), then earlier start.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass

from reelforge_core.models import AnalysisReport, ReelCandidate

PRESCORE_VERSION = "p1"
# A candidate edge within this many seconds of an utterance-unit edge counts
# as "on" it (scene cuts rarely coincide exactly with word timestamps).
BOUNDARY_EPS = 0.25
# Shortlist walk skips a candidate overlapping an already-kept one by more
# than this (intersection / shorter duration) — near-duplicate windows must
# not eat the whole shortlist, but the ranker still sees a few variants of a
# strong moment.
SHORTLIST_OVERLAP_MAX = 0.85


@dataclass(frozen=True)
class PrescoreFeatures:
    starts_on_unit_boundary: bool
    ends_on_unit_boundary: bool
    starts_mid_word: bool
    ends_mid_word: bool
    speech_ratio: float
    energy_peak_pos: float | None  # 0-1 position of the max energy z in-span
    energy_peak_z: float | None
    lufs_range: float
    n_scene_cuts: int
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


def compute_features(
    candidates: list[ReelCandidate], analysis: AnalysisReport
) -> dict[str, PrescoreFeatures]:
    """Features for every candidate, sharing one pass over the analysis."""
    from reelforge_core.reels.features import flatten_words
    from reelforge_core.reels.generators.moment import combined_scores
    from reelforge_core.reels.generators.sentence import build_units

    units = build_units(analysis.transcript)
    unit_starts = sorted(u.start for u in units)
    unit_ends = sorted(u.end for u in units)
    words = sorted(flatten_words(analysis.transcript))
    word_starts = [w[0] for w in words]
    # Prefix sums over word midpoints -> O(log n) spoken-time per span.
    mids = sorted(((s + e) / 2.0, e - s) for s, e in words)
    mid_ts = [m[0] for m in mids]
    spoken_prefix = [0.0]
    for _, d in mids:
        spoken_prefix.append(spoken_prefix[-1] + d)
    scored = combined_scores(analysis)  # [(time_sec, combined z)]
    score_ts = [t for t, _ in scored]
    lufs = sorted((p.time_sec, p.lufs) for p in analysis.loudness if p.lufs > -79.9)
    lufs_ts = [t for t, _ in lufs]
    cut_ts = sorted(s.start_sec for s in analysis.scenes)

    def _near(t: float, edges: list[float]) -> bool:
        i = bisect_left(edges, t)
        for j in (i - 1, i):
            if 0 <= j < len(edges) and abs(edges[j] - t) <= BOUNDARY_EPS:
                return True
        return False

    def _mid_word(t: float) -> bool:
        i = bisect_right(word_starts, t) - 1
        return i >= 0 and words[i][0] < t < words[i][1]

    out: dict[str, PrescoreFeatures] = {}
    for c in candidates:
        start, end = c.start_sec, c.end_sec
        dur = max(1e-6, end - start)
        spoken = (
            spoken_prefix[bisect_right(mid_ts, end)]
            - spoken_prefix[bisect_left(mid_ts, start)]
        )
        peak_pos: float | None = None
        peak_z: float | None = None
        lo, hi = bisect_left(score_ts, start), bisect_right(score_ts, end)
        if hi > lo:
            t_peak, z_peak = max(scored[lo:hi], key=lambda p: p[1])
            peak_pos = (t_peak - start) / dur
            peak_z = z_peak
        llo, lhi = bisect_left(lufs_ts, start), bisect_right(lufs_ts, end)
        span_lufs = [v for _, v in lufs[llo:lhi]]
        lufs_range = max(span_lufs) - min(span_lufs) if len(span_lufs) >= 2 else 0.0
        n_cuts = bisect_left(cut_ts, end) - bisect_right(cut_ts, start)
        out[c.candidate_id] = PrescoreFeatures(
            starts_on_unit_boundary=_near(start, unit_starts),
            ends_on_unit_boundary=_near(end, unit_ends),
            starts_mid_word=_mid_word(start),
            ends_mid_word=_mid_word(end),
            speech_ratio=round(spoken / dur, 4),
            energy_peak_pos=None if peak_pos is None else round(peak_pos, 4),
            energy_peak_z=None if peak_z is None else round(peak_z, 4),
            lufs_range=round(lufs_range, 2),
            n_scene_cuts=n_cuts,
            source=c.source,
        )
    return out


def prescore(f: PrescoreFeatures) -> float:
    """The documented linear formula. Pure."""
    s = 0.0
    if f.starts_on_unit_boundary:
        s += 25.0
    if f.ends_on_unit_boundary:
        s += 15.0
    if f.starts_mid_word:
        s -= 40.0
    if f.ends_mid_word:
        s -= 25.0
    if f.energy_peak_z is not None:
        s += 10.0 * min(f.energy_peak_z, 3.0)
    if f.energy_peak_pos is not None and f.energy_peak_pos < 0.2:
        s += 15.0
    s += 5.0 * min(f.n_scene_cuts, 4)
    if f.speech_ratio > 0.5:
        s += 10.0
    return round(s, 3)


def _time_overlap(a: ReelCandidate, b: ReelCandidate) -> float:
    inter = min(a.end_sec, b.end_sec) - max(a.start_sec, b.start_sec)
    shorter = max(1e-6, min(a.duration_sec, b.duration_sec))
    return max(0.0, inter) / shorter


def shortlist(
    candidates: list[ReelCandidate],
    features: dict[str, PrescoreFeatures],
    n: int,
) -> list[ReelCandidate]:
    """Top-n by prescore with a light overlap penalty: walk in prescore order,
    skipping a candidate that overlaps an already-kept one by more than
    SHORTLIST_OVERLAP_MAX. Returned in prescore order."""
    order = sorted(
        candidates,
        key=lambda c: (-prescore(features[c.candidate_id]), c.duration_sec, c.start_sec),
    )
    kept: list[ReelCandidate] = []
    for c in order:
        if len(kept) >= n:
            break
        if any(_time_overlap(c, k) > SHORTLIST_OVERLAP_MAX for k in kept):
            continue
        kept.append(c)
    return kept
