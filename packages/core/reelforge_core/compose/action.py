"""Action cutting (pro-editing CP8): where the cuts go in a hype edit.

The v1 hype grammar cut a span into ~2.6s pieces wherever the beat fell —
straight through a landing if that's where the beat was, and the skate
reel's pieces of one continuous shot read as no cut at all (17 planned,
2 visible). Here the ACTION decides:

- Detected action events (reels/events.py) are never cut through. Each gets
  its own shot that cuts in at the motion low just before it (the calm
  before the move) and holds FOLLOW_SEC after it.
- Between events, filler is cut short (~FILL_TARGET_SEC), on the beat.
- Every cut point is snapped to a beat within BEAT_SNAP_SEC — unless that
  would land inside an event or starve a piece.
- The strongest event (the money shot) gets a stepped speed ramp into the
  impact — 1.0 -> 0.7 -> 0.5 — pushing in each step, then back to 1.0 AT
  the impact so its sound plays, held >= PAYOFF_HOLD_SEC.
- Consecutive pieces alternate framing, so every cut is visible.

Pure; both the scene-mode grammar (styles._plan_hype) and the mix planner
use `action_pieces`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

FILL_TARGET_SEC = 2.0
FILL_MIN_SEC = 1.0
LEADIN_MAX_SEC = 2.0  # how far before an event its cut-in may reach
LEADIN_DEFAULT_SEC = 0.5
FOLLOW_SEC = 0.8
EVENT_MAX_STATIC_SEC = 2.5  # a long event reframes (keys) instead of cutting
BEAT_SNAP_SEC = 0.25
MIN_PIECE_SEC = 0.6
PAYOFF_HOLD_SEC = 2.0
RAMP = ((1.2, 0.7, 1.15), (0.6, 0.5, 1.3))  # (seconds before impact, speed, zoom)
ALT_ZOOM = 1.2


@dataclass
class Piece:
    start: float  # source seconds
    end: float
    speed: float = 1.0
    zoom: float = 1.0
    kind: str = "fill"  # fill | event | ramp | impact | locked
    keys: tuple = field(default_factory=tuple)

    @property
    def duration(self) -> float:
        return (self.end - self.start) / self.speed


def _inside_event(t: float, events: Sequence) -> bool:
    return any(ev.start_sec + 1e-3 < t < ev.end_sec - 1e-3 for ev in events)


def cut_in(ev, floor: float, activity: Callable[[float], float | None]) -> float:
    """The motion low in the LEADIN_MAX_SEC before the event, not before
    `floor`. Activity is per second (bins at i+0.5), so the cut goes at the
    start of the quietest bin. Pure."""
    lo = max(floor, ev.start_sec - LEADIN_MAX_SEC)
    if ev.start_sec - lo < 0.3:
        return max(floor, ev.start_sec - min(LEADIN_DEFAULT_SEC, ev.start_sec - floor))
    best_t, best_a = max(floor, ev.start_sec - LEADIN_DEFAULT_SEC), None
    t = float(int(lo))
    while t < ev.start_sec:
        if t >= lo - 1e-6:
            a = activity(t + 0.5)
            # Ties go to the LATER bin: the tightest cut-in into the move.
            if a is not None and (best_a is None or a <= best_a):
                best_t, best_a = t, a
        t += 1.0
    return min(best_t, ev.start_sec)


def _fill(s: float, e: float) -> list[Piece]:
    """Split [s, e] into ~FILL_TARGET_SEC pieces (the last absorbs the rest)."""
    if e - s <= 0:
        return []
    n = max(1, int(round((e - s) / FILL_TARGET_SEC)))
    step = (e - s) / n
    if n > 1 and step < FILL_MIN_SEC:
        n = max(1, int((e - s) // FILL_MIN_SEC))
        step = (e - s) / n
    return [Piece(round(s + i * step, 3), round(s + (i + 1) * step if i < n - 1 else e, 3)) for i in range(n)]


def _event_piece(start: float, end: float) -> Piece:
    """One uncut event shot; a long one reframes every ~2s instead."""
    p = Piece(start, end, kind="event")
    if end - start > EVENT_MAX_STATIC_SEC:
        n = int((end - start) // 2.0)
        p.keys = tuple(
            (round(i * (end - start) / (n + 1), 3), 1.0, 0.5, 0.5)  # zooms set later
            for i in range(n + 1)
        )
    return p


def _ramp(start: float, peak: float, end: float) -> list[Piece]:
    """1.0 -> 0.7 -> 0.5 into the impact, back to 1.0 at it."""
    out: list[Piece] = []
    first_step = peak - RAMP[0][0]
    if first_step - start >= MIN_PIECE_SEC:
        out.append(Piece(start, round(first_step, 3), kind="ramp"))
    else:
        first_step = start
    edges = [first_step] + [peak - before for before, _, _ in RAMP[1:]] + [peak]
    for (before, speed, zoom), a, b in zip(RAMP, edges, edges[1:]):
        if b - a > 0.1:
            out.append(Piece(round(a, 3), round(b, 3), speed=speed, zoom=zoom, kind="ramp"))
    out.append(Piece(round(peak, 3), round(end, 3), kind="impact"))
    return out


def snap_cuts(
    pieces: list[Piece],
    mezz_start: float,
    grid,
    events: Sequence,
) -> list[Piece]:
    """Move each interior cut so it lands on a beat of the mezzanine clock,
    within BEAT_SNAP_SEC, never into an event, never starving a piece. Pure."""
    if grid is None or len(pieces) < 2:
        return pieces
    out = [Piece(p.start, p.end, p.speed, p.zoom, p.kind, p.keys) for p in pieces]
    mezz = mezz_start
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        mezz_end = mezz + a.duration
        shift_mezz = grid.snap(mezz_end) - mezz_end
        if abs(shift_mezz) <= BEAT_SNAP_SEC and a.speed == 1.0 and b.speed == 1.0 and a.end == b.start:
            t = round(a.end + shift_mezz, 3)
            if (
                not _inside_event(t, events)
                and t - a.start >= MIN_PIECE_SEC
                and b.end - t >= MIN_PIECE_SEC
                and a.kind != "impact"
            ):
                a.end = b.start = t
        mezz += a.duration
    return out


def action_pieces(
    s: float,
    e: float,
    events: Sequence,
    activity: Callable[[float], float | None],
    *,
    grid=None,
    mezz_start: float = 0.0,
    money=None,
    prev_zoom: float = ALT_ZOOM,
) -> tuple[list[Piece], float]:
    """Cut one source span [s, e]. `money` is the reel's strongest event
    (the ramp goes on it, if it lies here). Returns (pieces, the zoom the
    last piece ends on). Pure."""
    evs = sorted(
        (ev for ev in events if ev.end_sec > s + 1e-3 and ev.start_sec < e - 1e-3),
        key=lambda ev: ev.start_sec,
    )
    pieces: list[Piece] = []
    cursor = s
    i = 0
    while i < len(evs):
        ev = evs[i]
        ci = cut_in(ev, cursor, activity) if ev.start_sec > cursor else cursor
        if ci - cursor < FILL_MIN_SEC:
            ci = cursor  # too little filler to stand alone: the event shot starts early
        pieces += _fill(cursor, ci)
        # The ramp only goes where the impact IS: an event straddling a scene
        # boundary would otherwise ramp toward a peak outside this span.
        ramp = (
            money is not None
            and ev is money
            and s <= money.peak_sec < e - 0.1
            and money.peak_sec - ci >= 0.6 + 0.1
        )
        end = min(e, max(ev.end_sec + FOLLOW_SEC, cursor))
        if ramp:
            end = min(e, max(end, money.peak_sec + PAYOFF_HOLD_SEC))
        # A following event that starts before this shot ends joins it —
        # ending the shot at `end` would cut straight through it.
        j = i + 1
        while j < len(evs) and evs[j].start_sec < end - 1e-3:
            end = min(e, max(end, evs[j].end_sec + FOLLOW_SEC))
            j += 1
        if ramp:
            pieces += _ramp(ci, money.peak_sec, end)
        else:
            pieces.append(_event_piece(ci, end))
        cursor = end
        i = j
    if e - cursor > 1e-3:
        tail = _fill(cursor, e)
        if pieces and tail and tail[-1].end - tail[0].start < FILL_MIN_SEC:
            pieces[-1].end = e  # a sliver after the last event joins it
        else:
            pieces += tail
    pieces = [p for p in pieces if p.end - p.start > 0.05]
    pieces = snap_cuts(pieces, mezz_start, grid, evs)
    # Alternate framing across consecutive pieces so each cut shows; ramp
    # steps keep their push-in, the impact snaps back wide.
    def other(z: float) -> float:
        return 1.0 if z > 1.0 else ALT_ZOOM

    zoom = prev_zoom
    for p in pieces:
        zoom = p.zoom if (p.kind == "ramp" and p.speed != 1.0) else other(zoom)
        p.zoom = zoom
        if len(p.keys) > 1:
            # A long event: its reframes alternate starting from this zoom.
            seq = [zoom if i % 2 == 0 else other(zoom) for i in range(len(p.keys))]
            p.keys = tuple((t, seq[i], cx, cy) for i, (t, _z, cx, cy) in enumerate(p.keys))
            zoom = seq[-1]
        else:
            p.keys = ((0.0, zoom, 0.5, 0.5),)
    return pieces, zoom


def money_event(events: Sequence, spans: Sequence[tuple[float, float]]):
    """The strongest event lying (at its peak) inside any of the spans."""
    inside = [ev for ev in events if any(s <= ev.peak_sec <= e for s, e in spans)]
    return max(inside, key=lambda ev: ev.strength, default=None)


def event_cold_open(start: float, end: float, events: Sequence) -> tuple[float, float] | None:
    """A cold open taken from the strongest event peak inside [start, end]
    (not its first 5s): 1.5s before the peak to 1.0s after, clamped into
    the span. For action footage the ranker may not propose one — no speech
    is needed to know where the moment is. Pure."""
    ev = money_event(events, [(start + 5.0, end)])
    if ev is None:
        return None
    cs, ce = max(start + 5.0, ev.peak_sec - 1.5), min(end, ev.peak_sec + 1.0)
    if ce - cs < 1.0:
        return None
    return (round(cs, 3), round(ce, 3))
