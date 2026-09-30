"""Pure measurements of a finished edit. No I/O — the scorecard gathers inputs.

"Visible change" is read from the EDIT PLAN, not from pixels. Calibration on
real renders (2026-09-30) showed ffmpeg's scene score can't make this call:
a genuinely visible cut scored 0.042 while a near-identical junction in a
static shot scored 0.089 and a handheld pan 0.116. The pipeline knows what it
did at every junction — a different clip, a jump in source time, a change of
framing — so that is what gets counted, and each change says why it counts.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

# A same-clip junction reads as a cut only if the source jumps at least this
# far; below it the picture barely moves (measured on the skate reel: 0.14s
# and -0.34s joins were indistinguishable from continuous footage).
SOURCE_JUMP_SEC = 0.5
# A zoom step this large is plainly visible (a 1.25x punch-in scored 13x its
# neighbours in calibration; 1.1x is the smallest step editors use).
REFRAME_ZOOM_STEP = 0.1

_GREETING = re.compile(
    r"\b(hey|hi|hello)\s+(guys|everyone|everybody|y'all|there)\b"
    r"|\bwhat'?s\s+up\b|\bwelcome\s+(back|to)\b|\bso\s+today\b|\bin\s+today'?s\s+video\b",
    re.IGNORECASE,
)
_FILLERS = {"um", "uh", "uhm", "umm", "erm", "er", "hmm", "mm"}
_FILLER_PHRASES = (("you", "know"), ("i", "mean"))
_TRAILING = re.compile(
    r"(so\s+yeah|and\s+yeah|yeah\s+so|anyway|anyways|that'?s\s+(it|about\s+it|all)"
    r"|or\s+whatever|and\s+stuff|or\s+something|you\s+know)\W*$",
    re.IGNORECASE,
)
# Shared with selection (reels/rank.py hook features, refine.py tail trim).
GREETING_RE = _GREETING
TRAILING_RE = _TRAILING
FILLER_WORDS = _FILLERS


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    shot: int | None = None  # which shot it was spoken in (None: unknown)


@dataclass(frozen=True)
class Shot:
    asset_id: str | None
    in_ts: float | None
    out_ts: float | None
    duration: float
    zoom: float = 1.0
    is_photo: bool = False
    transition_sec: float = 0.0  # crossfade INTO the next shot
    transition_kind: str | None = None
    # (t, zoom) framing changes within the shot, t from its start.
    framing: tuple[tuple[float, float], ...] = ()

    @property
    def zoom_in(self) -> float:
        """Framing at the shot's first frame."""
        return self.framing[0][1] if self.framing else self.zoom

    @property
    def zoom_out(self) -> float:
        """Framing at the shot's last frame."""
        return self.framing[-1][1] if self.framing else self.zoom


@dataclass(frozen=True)
class Change:
    t: float
    why: str  # cut | jump | reframe | transition | broll


# Transitions the eye sees even between identical frames: the picture moves
# or dips. Pros use them sparingly; a dissolve or one-frame "cut" between
# continuous footage shows nothing.
FLASHY_TRANSITIONS = frozenset(
    {
        "slideleft", "slideright", "slideup", "slidedown",
        "wipeleft", "wiperight", "smoothleft", "smoothright",
        "circleopen", "circleclose", "fadeblack", "fadewhite",
    }
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", text.lower())


# --- picture -------------------------------------------------------------------


def junction_times(shots: list[Shot]) -> list[float]:
    """Mezzanine time of each junction (the midpoint of its crossfade)."""
    out: list[float] = []
    start = 0.0
    for i in range(len(shots) - 1):
        x = shots[i].transition_sec
        nxt = start + shots[i].duration - x
        out.append(nxt + x / 2.0)
        start = nxt
    return out


def classify_junction(a: Shot, b: Shot) -> str:
    """cut | jump | reframe | transition | invisible — the content change
    first; a flashy transition only matters when nothing else changed."""
    if a.is_photo or b.is_photo or a.asset_id != b.asset_id:
        return "cut"
    if a.in_ts is None or a.out_ts is None or b.in_ts is None:
        return "cut"
    if abs(b.in_ts - a.out_ts) >= SOURCE_JUMP_SEC:
        return "jump"
    if abs(b.zoom_in - a.zoom_out) >= REFRAME_ZOOM_STEP:
        return "reframe"
    if a.transition_kind in FLASHY_TRANSITIONS:
        return "transition"
    return "invisible"


def flashy_share(shots: list[Shot]) -> float | None:
    """Share of junctions that slide, wipe or dip. Pros cut."""
    joins = shots[:-1]
    if not joins:
        return None
    return round(sum(1 for s in joins if s.transition_kind in FLASHY_TRANSITIONS) / len(joins), 3)


def extra_flashy(shots: list[Shot]) -> int | None:
    """Slides, wipes and dips beyond the budget: one per reel, or 10% of the
    joins on a long one — the same allowance the director is held to."""
    joins = shots[:-1]
    if not joins:
        return None
    used = sum(1 for s in joins if s.transition_kind in FLASHY_TRANSITIONS)
    return max(0, used - max(1, int(0.1 * len(joins))))


def visible_changes(
    shots: list[Shot],
    layers: list[tuple[float, float]] | None = None,
    duration: float | None = None,
) -> tuple[list[Change], int]:
    """(visible changes in time order, junctions that changed nothing)."""
    changes: list[Change] = []
    invisible = 0
    for t, (a, b) in zip(junction_times(shots), zip(shots, shots[1:])):
        kind = classify_junction(a, b)
        if kind == "invisible":
            invisible += 1
        else:
            changes.append(Change(round(t, 3), kind))
    # Reframes WITHIN a shot (framing keys), at their mezzanine time.
    start = 0.0
    for i, shot in enumerate(shots):
        prev_zoom = shot.framing[0][1] if shot.framing else shot.zoom
        for t, zoom in shot.framing[1:]:
            if abs(zoom - prev_zoom) >= REFRAME_ZOOM_STEP and 0 < t < shot.duration:
                changes.append(Change(round(start + t, 3), "reframe"))
            prev_zoom = zoom
        if i < len(shots) - 1:
            start += shot.duration - shot.transition_sec
    end = duration if duration is not None else sum(s.duration for s in shots)
    for start, stop in layers or []:
        # B-roll cuts in and cuts back out: two changes, each inside the reel.
        for t in (start, stop):
            if 0.05 < t < end - 0.05:
                changes.append(Change(round(t, 3), "broll"))
    changes.sort(key=lambda c: c.t)
    return changes, invisible


def changes_per_minute(changes: list[Change], duration: float) -> float:
    return round(len(changes) / duration * 60.0, 1) if duration > 0 else 0.0


def longest_static(changes: list[Change], duration: float) -> float:
    marks = [0.0, *[c.t for c in changes], duration]
    return round(max((b - a for a, b in zip(marks, marks[1:])), default=duration), 2)


# --- speech ---------------------------------------------------------------------


def speech_ratio(words: list[Word], duration: float) -> float:
    if duration <= 0:
        return 0.0
    return round(sum(max(0.0, w.end - w.start) for w in words) / duration, 3)


def hook_latency(words: list[Word]) -> float | None:
    """Seconds until the first spoken word, or None when nothing is said."""
    return round(words[0].start, 2) if words else None


def greeting_in_opening(words: list[Word], window: float = 3.0) -> str | None:
    opening = " ".join(w.text for w in words if w.start < window)
    m = _GREETING.search(opening)
    return m.group(0) if m else None


def dead_air(words: list[Word], gap: float = 0.4) -> dict:
    """Stalls: silences between two words spoken IN THE SAME SHOT — the
    speaker paused and nothing cut it out. A gap across a cut is an edit (a
    demo shot with no talking is showing, not stalling), so it isn't counted
    when shots are known."""
    pairs = [
        (a, b)
        for a, b in zip(words, words[1:])
        if a.shot is None or b.shot is None or a.shot == b.shot
    ]
    gaps = [round(b.start - a.end, 2) for a, b in pairs if b.start - a.end > gap]
    # Speaking time = each shot's run from its first word to its last.
    runs: dict = {}
    for w in words:
        first, last = runs.get(w.shot, (w.start, w.end))
        runs[w.shot] = (min(first, w.start), max(last, w.end))
    span = sum(b - a for a, b in runs.values()) if len(words) > 1 else 0.0
    return {
        "count": len(gaps),
        "longest": max(gaps, default=0.0),
        "percent": round(sum(gaps) / span * 100.0, 1) if span > 0 else 0.0,
    }


def filler_rate(words: list[Word], duration: float) -> float:
    tokens = [_norm(w.text) for w in words]
    count = sum(1 for t in tokens if t in _FILLERS)
    for i in range(len(tokens) - 1):
        if (tokens[i], tokens[i + 1]) in _FILLER_PHRASES:
            count += 1
    return round(count / duration * 60.0, 2) if duration > 0 else 0.0


def ending(words: list[Word], duration: float) -> dict:
    """Does the reel end on a line that lands?"""
    if not words:
        return {"applies": False}
    tail_text = " ".join(w.text for w in words[-5:]).strip()
    last = words[-1]
    return {
        "applies": True,
        "last_word_end": round(last.end, 2),
        "tail_after_last_word": round(duration - last.end, 2),
        "trailing_filler": bool(_TRAILING.search(tail_text)),
        "ends_on_sentence": last.text.rstrip().endswith((".", "!", "?")),
        "closing_words": tail_text,
    }


# --- captions -------------------------------------------------------------------


def caption_stats(boxes: list, safe) -> dict:
    """Word density, highlight share and safe-zone fit for caption events.

    Karaoke repeats one line as several events (one per highlighted word), so
    consecutive events with the same text are one caption.
    """
    speech = [b for b in boxes if b.style != "Overlay"]
    groups: list[list] = []
    for b in speech:
        if groups and groups[-1][-1].text == b.text and abs(groups[-1][-1].end - b.start) < 0.05:
            groups[-1].append(b)
        else:
            groups.append([b])
    words_per = [g[0].words for g in groups if g[0].words]
    words_total = sum(words_per)
    highlighted_total = sum(len({w for b in g for w in b.highlighted}) for g in groups)
    violations = [
        {"t": round(b.start, 2), "text": b.text[:60], "style": b.style,
         "box": [round(b.rect.x0), round(b.rect.y0), round(b.rect.x1), round(b.rect.y1)]}
        for b in boxes
        if b.text.strip() and not safe.contains(b.rect, tolerance=2.0)
    ]
    return {
        "captions": len(groups),
        "overlays": len(boxes) - len(speech),
        "words_per_caption_p95": _p95(words_per),
        "words_per_caption_max": max(words_per, default=0),
        "highlighted_share": round(highlighted_total / words_total, 3) if words_total else 0.0,
        "safe_zone_violations": len(violations),
        "violations": violations[:10],
    }


def _p95(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    return float(statistics.quantiles(ordered, n=20, method="inclusive")[-1])


# --- what kind of reel this is ---------------------------------------------------

LONG_FORM_SEC = 180.0
TALKING_SPEECH_RATIO = 0.35


def content_kind(duration: float, ratio: float, style: str | None) -> str:
    """talking | action | long_form — which targets the reel is held to."""
    if duration >= LONG_FORM_SEC:
        return "long_form"
    if style == "talking_head" or ratio >= TALKING_SPEECH_RATIO:
        return "talking"
    return "action"
