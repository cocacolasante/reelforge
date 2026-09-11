"""Action events + the cut guard (pure, no I/O).

Speech- and scene-aligned candidate edges know nothing about what is happening
on screen. On action footage that is exactly backwards: people talk BEFORE the
action ("here it comes") and react AFTER it ("it just smashed me"), so
speech-bounded cuts land right before a wave hits or right after the fall.

`detect_events` turns the per-second motion track and the loudness curve into
event spans — sustained activity well above the clip's own baseline — whose
starts are then walked back through the visible onset (a wave rises for a few
seconds before it hits the camera). `guard_span` moves a span's edges so none of
them slices an event:

- an END may not fall inside an event or within ANTICIPATION_SEC before one
  (the viewer sees the build-up, then the cut), and needs FOLLOW_SEC of
  follow-through after it;
- a START may not fall inside an event, within LEAD_SEC before one (no
  build-up), or within AFTERMATH_SEC after one (opens on the aftermath).

Fixes prefer INCLUDING a threatened event (moving the edge past it) over
dropping it, within the duration window and MAX_GUARD_SHIFT_SEC. A span that
can't be fixed comes back unchanged with status "violation". Moved edges never
land mid-word (same rule as compose/speech_snap.py, trying both word edges).

Thresholds were tuned against hand-labeled events on real GoPro surf footage
(10 of 11 labeled events found, no false positives) and checked for sparsity
on continuous snowboarding footage. The live selection run (2026-09-11) showed
detected starts trailing the visible action by up to 4s — ONSET_* closes most
of that gap.
"""

from __future__ import annotations

import logging
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass
from typing import Sequence

from reelforge_core.models import AnalysisReport, ReelCandidate, SelectionConfig

log = logging.getLogger(__name__)

# --- detection ---------------------------------------------------------------
EVENT_HI = 3.0  # activity that seeds an event
EVENT_LO = 2.0  # activity that extends one (hysteresis)
MERGE_GAP_SEC = 1.0  # events separated by at most this merge
# After merging, each start walks back through activity >= EVENT_ONSET for at
# most ONSET_MAX_BACK_SEC — never into the previous event.
EVENT_ONSET = 1.0
ONSET_MAX_BACK_SEC = 3
LOUD_DB_PER_UNIT = 3.0  # loudness prominence (dB over the local median) per unit
LOUD_BASELINE_HALF_WIN = 15  # bins each side for the rolling loudness median
SPEECH_COVER_MIN = 0.5  # a bin with at least this many spoken seconds...
SPEECH_LOUD_DISCOUNT = 0.5  # ...counts its loudness prominence at this weight
MOTION_SCALE_FLOOR = 1.0  # robust-z denominator floor for near-flat footage
SILENCE_LUFS = -79.9

# --- guard -------------------------------------------------------------------
ANTICIPATION_SEC = 4.0
FOLLOW_SEC = 1.5
LEAD_SEC = 3.0
AFTERMATH_SEC = 1.5
MAX_GUARD_SHIFT_SEC = 8.0
# Resolving a cut by DROPPING the threatened event costs as much as the largest
# allowed edge move — so including the event wins whenever it is reachable.
EXCLUDE_PENALTY_SEC = MAX_GUARD_SHIFT_SEC
SNAP_MAX_NUDGE_SEC = 0.6
# Bounds are stored at ms precision: within this of the footage's first/last
# frame counts as that edge, and of an event edge as containing it (a reel
# ending at round(duration, 3) still contains an event that runs to the end).
BOUND_TOL_SEC = 0.01


@dataclass(frozen=True)
class ActionEvent:
    start_sec: float
    end_sec: float
    peak_sec: float
    strength: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GuardResult:
    start: float
    end: float
    status: str  # "ok" | "moved" | "violation"


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


def activity_track(analysis: AnalysisReport) -> list[float]:
    """Per-second activity: the larger of robust motion z and loudness
    prominence (dB over the rolling median, speech-discounted). Pure."""
    n = max(len(analysis.energy), len(analysis.loudness))
    if n == 0:
        return []

    motion: list[float | None] = [None] * n
    for p in analysis.energy:
        i = int(p.time_sec)
        if 0 <= i < n:
            motion[i] = p.motion
    lufs: list[float | None] = [None] * n
    for p in analysis.loudness:
        i = int(p.time_sec)
        if 0 <= i < n and p.lufs > SILENCE_LUFS:
            lufs[i] = p.lufs

    zm = [0.0] * n
    present = [m for m in motion if m is not None]
    if present:
        med = statistics.median(present)
        mad = statistics.median([abs(m - med) for m in present])
        scale = max(1.4826 * mad, MOTION_SCALE_FLOOR)
        zm = [(m - med) / scale if m is not None else 0.0 for m in motion]

    # Loud nearby voices are not action: discount loudness in spoken bins.
    spoken = [0.0] * n
    segments = analysis.transcript.segments if analysis.transcript is not None else []
    for seg in segments:
        for w in seg.words:
            for i in range(max(0, int(w.start)), min(n, int(w.end) + 1)):
                spoken[i] += max(0.0, min(w.end, i + 1) - max(w.start, i))

    zl = [0.0] * n
    for i in range(n):
        if lufs[i] is None:
            continue
        lo, hi = max(0, i - LOUD_BASELINE_HALF_WIN), i + LOUD_BASELINE_HALF_WIN + 1
        window = [x for x in lufs[lo:hi] if x is not None]
        prominence = (lufs[i] - statistics.median(window)) / LOUD_DB_PER_UNIT
        if spoken[i] >= SPEECH_COVER_MIN:
            prominence *= SPEECH_LOUD_DISCOUNT
        zl[i] = prominence

    return [max(a, b) for a, b in zip(zm, zl)]


def detect_events(analysis: AnalysisReport) -> list[ActionEvent]:
    """Event spans from the activity track: seeded at EVENT_HI, extended while
    >= EVENT_LO, merged across MERGE_GAP_SEC gaps, then each start walked back
    through the onset. Ascending by start; bounds at ms precision. Pure."""
    act = activity_track(analysis)
    n = len(act)
    spans: list[list] = []  # [start_bin, end_bin_exclusive, peak_bin, strength]
    i = 0
    while i < n:
        if act[i] < EVENT_HI:
            i += 1
            continue
        s = i
        while s - 1 >= 0 and act[s - 1] >= EVENT_LO:
            s -= 1
        e = i
        while e + 1 < n and act[e + 1] >= EVENT_LO:
            e += 1
        peak = max(range(s, e + 1), key=lambda k: act[k])
        spans.append([s, e + 1, peak, act[peak]])
        i = e + 1

    merged: list[list] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= MERGE_GAP_SEC:
            prev = merged[-1]
            stronger = prev if prev[3] >= span[3] else span
            merged[-1] = [prev[0], span[1], stronger[2], stronger[3]]
        else:
            merged.append(span)

    prev_end = 0
    for span in merged:
        k = span[0]
        while k - 1 >= prev_end and span[0] - (k - 1) <= ONSET_MAX_BACK_SEC and act[k - 1] >= EVENT_ONSET:
            k -= 1
        span[0] = k
        prev_end = span[1]

    duration = analysis.duration
    return [
        ActionEvent(
            start_sec=float(s),
            end_sec=round(min(float(e), duration), 3),
            peak_sec=round(min(p + 0.5, duration), 3),
            strength=round(st, 3),
        )
        for s, e, p, st in merged
    ]


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------


def _start_hits(t: float, ev: ActionEvent) -> bool:
    return ev.start_sec - LEAD_SEC < t < ev.end_sec + AFTERMATH_SEC


def _end_hits(t: float, ev: ActionEvent) -> bool:
    return ev.start_sec - ANTICIPATION_SEC < t < ev.end_sec + FOLLOW_SEC


def edge_ok(t: float, kind: str, events: Sequence[ActionEvent], duration: float) -> bool:
    """Whether a START (kind="start") or END (kind="end") at `t` cuts no
    event. The footage's own first/last frame can't cut anything off. Pure."""
    if kind == "start":
        return t <= BOUND_TOL_SEC or not any(_start_hits(t, ev) for ev in events)
    return t >= duration - BOUND_TOL_SEC or not any(_end_hits(t, ev) for ev in events)


def event_inside(ev: ActionEvent, start: float, end: float) -> bool:
    """Whether the whole event lies within [start, end] (ms tolerance). Pure."""
    return start - BOUND_TOL_SEC <= ev.start_sec and ev.end_sec <= end + BOUND_TOL_SEC


def event_position(ev: ActionEvent, start: float, end: float) -> str:
    """Where an event sits relative to [start, end]: inside | before_start |
    after_end | crosses_start | crosses_end | spans_clip. Pure."""
    if event_inside(ev, start, end):
        return "inside"
    if ev.end_sec <= start:
        return "before_start"
    if ev.start_sec >= end:
        return "after_end"
    if ev.start_sec < start and ev.end_sec > end:
        return "spans_clip"
    return "crosses_start" if ev.start_sec < start else "crosses_end"


def events_near(
    events: Sequence[ActionEvent], start: float, end: float, margin: float = 8.0
) -> list[dict]:
    """Model-facing view of the events within `margin` of a span. Pure."""
    return [
        {
            "start_sec": round(ev.start_sec, 2),
            "end_sec": round(ev.end_sec, 2),
            "peak_sec": round(ev.peak_sec, 2),
            "strength": round(ev.strength, 1),
            "where": event_position(ev, start, end),
        }
        for ev in events
        if ev.end_sec >= start - margin and ev.start_sec <= end + margin
    ]


def _word_safe(
    t: float, words: Sequence[tuple[float, float]], starts: list[float], kind: str
) -> tuple[float, ...]:
    """`t` itself when it isn't mid-word; otherwise the compose/speech_snap
    choice first (include a short partial word), then the word's other edge —
    the preferred snap can land back inside a no-cut window. Words sorted."""
    i = bisect_right(starts, t) - 1
    if i < 0:
        return (t,)
    ws, we = words[i]
    if not ws < t < we:
        return (t,)
    if kind == "start":
        preferred = ws if t - ws <= SNAP_MAX_NUDGE_SEC else we
    else:
        preferred = we if we - t <= SNAP_MAX_NUDGE_SEC else ws
    return (preferred, we if preferred == ws else ws)


def guard_span(
    start: float,
    end: float,
    events: Sequence[ActionEvent],
    *,
    min_sec: float,
    max_sec: float,
    duration: float,
    words: Sequence[tuple[float, float]] = (),
) -> GuardResult:
    """Move `[start, end]` so neither edge cuts an event. `words` must be
    sorted by start. Pure; see the module docstring for the rules."""
    if not events or (
        edge_ok(start, "start", events, duration) and edge_ok(end, "end", events, duration)
    ):
        return GuardResult(start, end, "ok")

    starts = [w[0] for w in words]
    threatened = [
        ev
        for ev in events
        if (start > BOUND_TOL_SEC and _start_hits(start, ev))
        or (end < duration - BOUND_TOL_SEC and _end_hits(end, ev))
    ]

    def _positions(orig: float, kind: str) -> list[float]:
        raw = {orig, 0.0 if kind == "start" else duration}
        for ev in events:
            if kind == "start":
                raw.update((ev.start_sec - LEAD_SEC, ev.end_sec + AFTERMATH_SEC))
            else:
                raw.update((ev.start_sec - ANTICIPATION_SEC, ev.end_sec + FOLLOW_SEC))
        out: set[float] = set()
        for p in raw:
            p = min(max(p, 0.0), duration)
            if abs(p - orig) > MAX_GUARD_SHIFT_SEC:
                continue
            for q in _word_safe(p, words, starts, kind):
                if edge_ok(q, kind, events, duration):
                    out.add(round(q, 3))
        return sorted(out)

    best: tuple[tuple[float, float, float], float, float] | None = None
    for s in _positions(start, "start"):
        for e in _positions(end, "end"):
            dur = e - s
            if dur < min_sec - BOUND_TOL_SEC or dur > max_sec + BOUND_TOL_SEC:
                continue
            # Legal edges can't sit inside an event, so each threatened event
            # is either fully inside the new span or fully outside it.
            excluded = sum(1 for ev in threatened if not event_inside(ev, s, e))
            cost = abs(s - start) + abs(e - end) + EXCLUDE_PENALTY_SEC * excluded
            key = (round(cost, 6), -round(dur, 6), s)
            if best is None or key < best[0]:
                best = (key, s, e)
    if best is None:
        return GuardResult(start, end, "violation")
    return GuardResult(best[1], best[2], "moved")


def guard_candidates(
    candidates: Sequence[ReelCandidate],
    analysis: AnalysisReport,
    config: SelectionConfig,
    events: Sequence[ActionEvent] | None = None,
) -> list[ReelCandidate]:
    """Apply `guard_span` to every candidate. Moved candidates get a fresh
    time-span identity + covering scenes (source kept); spans that collide
    after moving dedupe first-wins. Unfixable spans pass through unchanged."""
    from reelforge_core.compose.speech_snap import flatten_words
    from reelforge_core.reels.candidates import _candidate_id, _ms, covering_scenes

    events = detect_events(analysis) if events is None else events
    if not events:
        return list(candidates)
    words = sorted(flatten_words(analysis.transcript))
    out: list[ReelCandidate] = []
    seen: set[tuple[int, int]] = set()
    moved = violations = 0
    for c in candidates:
        g = guard_span(
            c.start_sec,
            c.end_sec,
            events,
            min_sec=config.effective_min_sec,
            max_sec=config.effective_max_sec,
            duration=analysis.duration,
            words=words,
        )
        if g.status == "moved":
            moved += 1
            covered = covering_scenes(analysis.scenes, g.start, g.end)
            c = ReelCandidate(
                candidate_id=_candidate_id(analysis.asset_id, g.start, g.end),
                scene_indices=covered,
                start_sec=g.start,
                end_sec=g.end,
                duration_sec=round(g.end - g.start, 6),
                scene_count=len(covered),
                source=c.source,
            )
        elif g.status == "violation":
            violations += 1
        key = (_ms(c.start_sec), _ms(c.end_sec))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    log.info(
        "event guard: %d events; moved %d/%d candidates, %d unfixable, %d merged",
        len(events),
        moved,
        len(candidates),
        violations,
        len(candidates) - len(out),
    )
    return out
