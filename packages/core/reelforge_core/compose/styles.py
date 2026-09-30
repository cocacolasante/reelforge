"""Editing-style grammars: the deterministic edit planner (pure).

A style is an editing GRAMMAR — how a kind of content wants to be cut — not a
color preset. The planner takes the scene-mode base bounds (already clamped/
trimmed/speech-snapped by clip_bounds) and rewrites them per style: beat-placed
cuts, speed ramps into energy peaks, punch-in alternation, jump cuts, Ken
Burns policy, per-cut transitions, and caption/music suggestions.

Activation rule (mirrors the smart-sentinel philosophy): a style other than
`classic` only engages in the true smart-auto flow — `smart_mode` on AND the
user left `transition.kind == "auto"` — or when `config.style` names one
explicitly. Manual configs keep today's behavior bit-for-bit.

Everything here is pure and deterministic; the AI edit-director (CP5) will
propose adjustments WITHIN these grammars' bounds.
"""

from __future__ import annotations

from dataclasses import replace, dataclass, field
from typing import TYPE_CHECKING

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.models import AnalysisReport, ComposeConfig, RankedReel

if TYPE_CHECKING:
    from reelforge_core.compose.silence import SpeechEnvelope

STYLE_NAMES = ("classic", "hype", "talking_head", "cinematic", "chill")

STYLE_DESCRIPTIONS = {
    "classic": "conservative: whole scenes, one transition kind",
    "hype": "beat-placed fast cuts, slow-mo on the peak, punch-ins",
    "talking_head": "jump cuts through dead air, punch-in variety, big captions",
    "cinematic": "long dissolves, dips to black, slow camera drift",
    "chill": "gentle long fades, minimal editing",
}

# Styles that bias music selection away from the reel's suggested mood.
MUSIC_MOOD_BIAS = {"hype": "energetic", "chill": "calm"}

# hype pacing
HYPE_LULL_Z = -0.2

# talking-head punch-in
TH_PUNCH_IN = 1.25
# Visual rhythm (CP3): a talking head's framing changes about every
# RHYTHM_INTERVAL_SEC, at a phrase boundary, never within RHYTHM_MIN_GAP_SEC
# of the last change or of the shot's end. Measured on real reels, the old
# "punch in on every other jump-cut piece" left 10-20s sentences as one frame.
RHYTHM_INTERVAL_SEC = 3.0
RHYTHM_MIN_GAP_SEC = 1.5
RHYTHM_FORCE_SEC = 3.6  # past this, any word boundary will do
RHYTHM_MAX_STATIC_SEC = 4.0  # the QA target; a longer tail gets an early key
RHYTHM_ZOOMS = (1.0, 1.15, 1.0, 1.3)  # wide, medium, wide, tight
PHRASE_GAP_SEC = 0.12
# Where to zoom in a portrait talking head: the face sits above centre.
FACE_CX, FACE_CY = 0.5, 0.42
HYPE_ALT_ZOOM = 1.2


def hype_alt_zoom(prev_zoom: float) -> float:
    """The framing for the next piece of a beat-split shot: the opposite of
    whatever the previous shot ENDED on — alternating by piece index let a
    piece after the slow-mo money shot (punched in to 1.2) open at 1.2 too,
    an invisible cut on an 8s static stretch."""
    return 1.0 if prev_zoom > 1.0 else HYPE_ALT_ZOOM

# heuristic auto-classification thresholds
SPEECH_RATIO_TALKY = 0.4
ENERGY_PEAK_HYPE_Z = 1.0


@dataclass(frozen=True)
class PlannedShot:
    scene_index: int
    in_ts: float
    out_ts: float
    speed: float = 1.0
    punch_in: float | None = None
    punch_in_animated: bool = False
    force_ken_burns: bool = False
    # Framing changes WITHIN the shot: (t, zoom, cx, cy) with t in output
    # seconds from the shot's start, zoom >= 1, (cx, cy) the crop centre as
    # fractions of the frame. Applied in the render graph (not a clip-cache
    # key) and never change the shot's duration. Takes precedence over
    # punch_in.
    framing_keys: tuple[tuple[float, float, float, float], ...] = ()

    @property
    def duration(self) -> float:
        return max(0.05, (self.out_ts - self.in_ts) / max(0.25, self.speed))


@dataclass
class EditPlan:
    style: str
    shots: list[PlannedShot]
    per_cut: list[tuple[str, float] | None]  # len n-1; None = reel default
    caption_mode: str | None = None  # suggestion; never un-mutes "off"
    caption_position: str | None = None
    notes: list[str] = field(default_factory=list)
    # CP6: (start, end) source seconds of the cold open prepended to this
    # plan, and how many leading shots it became. The director may not touch
    # those shots.
    cold_open: tuple[float, float] | None = None
    cold_open_shots: int = 0


# Cold opens (CP6, decision D4): action always leads with its peak; a talking
# head only when the payoff line is strong; calmer grammars never.
COLD_OPEN_PAYOFF_MIN = 70


def cold_open_for(config: ComposeConfig, style: str, reel: RankedReel) -> tuple[float, float] | None:
    """The reel's validated cold open if this render should use it. Pure."""
    if reel.cold_open is None or config.cold_open == "off":
        return None
    if config.cold_open == "on" or style == "hype":
        return tuple(reel.cold_open)  # type: ignore[return-value]
    if style == "talking_head" and reel.scores.emotional_payoff >= COLD_OPEN_PAYOFF_MIN:
        return tuple(reel.cold_open)  # type: ignore[return-value]
    return None


def with_cold_open(
    scene_bounds: list[tuple[int, float, float]],
    cold: tuple[float, float],
    analysis: AnalysisReport,
) -> list[tuple[int, float, float]]:
    """Prepend the cold open as its own shot. Pure."""
    cs, ce = cold
    idx = next(
        (sc.index for sc in analysis.scenes if sc.start_sec <= cs < sc.end_sec),
        scene_bounds[0][0] if scene_bounds else 0,
    )
    return [(idx, cs, ce), *scene_bounds]


def lock_cold_open(plan: EditPlan, cold: tuple[float, float]) -> EditPlan:
    """Mark the leading shots that came from the cold open (a grammar may
    have split it) and hard-cut out of it into the reel's real start."""
    cs, ce = cold
    k = 0
    for shot in plan.shots:
        if shot.in_ts >= cs - 1e-3 and shot.out_ts <= ce + 1e-3:
            k += 1
        else:
            break
    if k == 0 or k >= len(plan.shots):
        return plan
    per_cut = list(plan.per_cut)
    per_cut[k - 1] = ("cut", 0.04)
    notes = [*plan.notes, f"cold open {cs:.1f}-{ce:.1f}s first"]
    return replace(plan, per_cut=per_cut, cold_open=(cs, ce), cold_open_shots=k, notes=notes)


def resolve_style(config: ComposeConfig, reel: RankedReel, analysis: AnalysisReport) -> str:
    """Which grammar applies. Explicit style wins; otherwise only the true
    smart-auto flow gets auto-classification — manual flows stay classic."""
    if config.style != "auto":
        return config.style
    if not config.smart_mode or config.transition.kind != "auto":
        return "classic"
    # CP4 will persist the ranker's classification on the reel.
    ranked = getattr(reel, "edit_style", None)
    if ranked and ranked in STYLE_NAMES:
        return ranked
    return _heuristic_style(reel, analysis)


def _heuristic_style(reel: RankedReel, analysis: AnalysisReport) -> str:
    span = max(0.5, reel.end_sec - reel.start_sec)
    spoken = 0.0
    if analysis.transcript is not None:
        for seg in analysis.transcript.segments:
            for w in seg.words:
                if reel.start_sec <= (w.start + w.end) / 2.0 <= reel.end_sec:
                    spoken += w.end - w.start
    if spoken / span >= SPEECH_RATIO_TALKY:
        return "talking_head"
    peak = _peak_z_in_span(analysis, reel.start_sec, reel.end_sec)
    if peak is not None and peak >= ENERGY_PEAK_HYPE_Z:
        return "hype"
    return "cinematic"


def _energy_z(analysis: AnalysisReport) -> list[tuple[float, float]]:
    from reelforge_core.reels.generators.moment import combined_scores

    return combined_scores(analysis)


def _peak_z_in_span(analysis: AnalysisReport, start: float, end: float) -> float | None:
    vals = [z for t, z in _energy_z(analysis) if start <= t <= end]
    return max(vals) if vals else None


def plan_edit(
    style: str,
    scene_bounds: list[tuple[int, float, float]],
    reel: RankedReel,
    analysis: AnalysisReport,
    config: ComposeConfig,
    beat_grid: BeatGrid | None,
    envelope: "SpeechEnvelope | None" = None,
    cold_open: tuple[float, float] | None = None,
) -> EditPlan:
    """Rewrite the base shot plan per the style grammar. Pure, deterministic.
    `envelope` (the asset's measured speech activity) drives jump cuts; without
    it they fall back to transcript word gaps."""
    if style == "hype":
        plan = _plan_hype(scene_bounds, analysis, beat_grid, cold_open)
    elif style == "talking_head":
        plan = _plan_talking_head(scene_bounds, analysis, envelope)
    elif style == "cinematic":
        plan = _plan_cinematic(scene_bounds)
    elif style == "chill":
        plan = _plan_chill(scene_bounds)
    else:
        plan = EditPlan(
            style="classic",
            shots=[PlannedShot(i, s, e) for i, s, e in scene_bounds],
            per_cut=[None] * max(0, len(scene_bounds) - 1),
        )

    # Forced jump cuts apply to any style that didn't already do them
    # ("auto" is each grammar's own call; talking_head always does).
    if config.jump_cuts == "on" and style != "talking_head":
        plan = _with_jump_cuts(plan, analysis, envelope)
    return plan


# ---------------------------------------------------------------------------
# grammars
# ---------------------------------------------------------------------------


def _plan_hype(
    scene_bounds: list[tuple[int, float, float]],
    analysis: AnalysisReport,
    grid: BeatGrid | None,
    cold_open: tuple[float, float] | None = None,
) -> EditPlan:
    """Action-led cuts (compose/action.py): no cut through an action event,
    each event cut in at the motion low before it, short beat-snapped filler
    between, a stepped speed ramp into the strongest impact, alternating
    framing so every cut shows, speed-up through lulls, hard cuts only.
    A leading cold-open bound stays one untouched shot."""
    from reelforge_core.compose.action import Piece, action_pieces, money_event
    from reelforge_core.reels.events import activity_track, detect_events

    events = detect_events(analysis)
    act = activity_track(analysis)

    def activity(t: float) -> float | None:
        i = int(t)
        return act[i] if 0 <= i < len(act) else None

    energy = _energy_z(analysis)
    bounds = list(scene_bounds)
    locked = (
        cold_open is not None
        and bool(bounds)
        and abs(bounds[0][1] - cold_open[0]) < 1e-3
        and abs(bounds[0][2] - cold_open[1]) < 1e-3
    )
    body = bounds[1:] if locked else bounds
    money = money_event(events, [(s, e) for _, s, e in body])
    shots: list[PlannedShot] = []
    per_cut: list[tuple[str, float] | None] = []
    notes: list[str] = []
    mezz = 0.0
    prev_zoom = HYPE_ALT_ZOOM  # so the first alternated piece opens wide
    for bi, (idx, s, e) in enumerate(bounds):
        if bi == 0 and locked:
            pieces = [Piece(s, e, kind="locked", keys=((0.0, 1.0, 0.5, 0.5),))]
            prev_zoom = 1.0
        else:
            pieces, prev_zoom = action_pieces(
                s, e, events, activity, grid=grid, mezz_start=mezz, money=money,
                prev_zoom=prev_zoom,
            )
        for p in pieces:
            speed = p.speed
            if p.kind == "fill" and speed == 1.0 and p.end - p.start >= 2.0:
                zs = [z for t, z in energy if p.start <= t <= p.end]
                if zs and sum(zs) / len(zs) < HYPE_LULL_Z:
                    speed = 1.5
            shot = PlannedShot(idx, p.start, p.end, speed=speed, framing_keys=p.keys)
            if shots:
                # Hard cuts everywhere; pros slide rarely, and the director
                # may still spend its one flashy transition where it counts.
                per_cut.append(("cut", 0.04))
            shots.append(shot)
            mezz += shot.duration
    if money is not None and any(sh.speed < 1.0 for sh in shots):
        notes.append(f"speed ramp into the impact at {money.peak_sec:.1f}s")
    inside = [ev for ev in events if any(s <= ev.peak_sec <= e for _, s, e in body)]
    if inside:
        notes.append(f"{len(inside)} action event(s) kept whole")
    if len(shots) > len(bounds):
        notes.append(f"action cuts: {len(bounds)} shot(s) -> {len(shots)}")
    return EditPlan(style="hype", shots=shots, per_cut=per_cut, notes=notes)


def phrase_boundaries(words: list[tuple[float, float, str]]) -> list[tuple[float, bool]]:
    """(time, strong) cut points between words, in the shot's own output
    seconds: strong after a sentence end or a pause >= PHRASE_GAP_SEC, weak
    at every other word boundary. Pure."""
    out: list[tuple[float, bool]] = []
    for (s0, e0, w0), (s1, _e1, _w1) in zip(words, words[1:]):
        mid = (e0 + s1) / 2.0
        strong = w0.rstrip().endswith((".", "!", "?")) or s1 - e0 >= PHRASE_GAP_SEC
        out.append((round(mid, 3), strong))
    return out


def rhythm_keys(
    duration: float,
    words: list[tuple[float, float, str]],
    zoom_index: int,
    *,
    cx: float = FACE_CX,
    cy: float = FACE_CY,
    interval: float = RHYTHM_INTERVAL_SEC,
    force: float = RHYTHM_FORCE_SEC,
    max_static: float = RHYTHM_MAX_STATIC_SEC,
    zooms: tuple[float, ...] = RHYTHM_ZOOMS,
) -> tuple[tuple[tuple[float, float, float, float], ...], int]:
    """Framing keys for one talking-head shot, and the next index into
    RHYTHM_ZOOMS. The shot opens on the next framing in the cycle (so the
    cut INTO it is visible), then changes about every RHYTHM_INTERVAL_SEC at
    a phrase boundary or a word-free moment — or at any word boundary once
    RHYTHM_FORCE_SEC has passed without one. Pure; `words` are
    (start, end, text) in shot-output seconds."""
    keys = [(0.0, zooms[zoom_index % len(zooms)], cx, cy)]
    zoom_index += 1
    last = 0.0
    # Moments where nobody is speaking (a silent shot, a kept pause, before
    # the first word) are as good as a sentence end — without them a shot
    # with no speech would hold one framing for its whole length.
    quiet = [
        (g, True)
        for g in (round(x * 0.5, 3) for x in range(1, int(duration / 0.5) + 1))
        if not any(ws - 0.05 <= g <= we + 0.05 for ws, we, _ in words)
    ]
    for t, strong in sorted(phrase_boundaries(words) + quiet):
        if duration - t < RHYTHM_MIN_GAP_SEC:
            break
        since = t - last
        # The last key must not leave a long static tail: once the rest of
        # the shot is longer than RHYTHM_MAX_STATIC_SEC, take the first
        # boundary that leaves no more than RHYTHM_FORCE_SEC (keys can't sit
        # in the final RHYTHM_MIN_GAP_SEC, so waiting for the force would
        # overshoot).
        tail_too_long = (
            since >= RHYTHM_MIN_GAP_SEC
            and duration - last > max_static
            and duration - t <= force
        )
        if since >= interval and strong or since >= force or tail_too_long:
            keys.append((t, zooms[zoom_index % len(zooms)], cx, cy))
            zoom_index += 1
            last = t
    return tuple(keys), zoom_index


def _shot_words(
    analysis: AnalysisReport, in_ts: float, out_ts: float, speed: float = 1.0
) -> list[tuple[float, float, str]]:
    """The shot's words in its own output seconds."""
    if analysis.transcript is None:
        return []
    out = []
    for seg in analysis.transcript.segments:
        if seg.end < in_ts or seg.start > out_ts:
            continue
        for w in seg.words:
            if in_ts <= (w.start + w.end) / 2.0 <= out_ts and w.word.strip():
                out.append(((w.start - in_ts) / speed, (w.end - in_ts) / speed, w.word))
    return out


def _plan_talking_head(
    scene_bounds: list[tuple[int, float, float]],
    analysis: AnalysisReport,
    envelope: "SpeechEnvelope | None" = None,
) -> EditPlan:
    """Jump-cut the dead air, then keep the picture moving: every shot opens
    on the next framing in the cycle and reframes about every 3s at a phrase
    boundary (`rhythm_keys`). Punch captions in the safe lower band."""
    from reelforge_core.compose.jumpcuts import apply_jump_cuts

    shots_raw, cuts_raw = apply_jump_cuts(scene_bounds, analysis.transcript, envelope=envelope)
    shots: list[PlannedShot] = []
    zoom_index = 0
    for idx, s, e in shots_raw:
        keys, zoom_index = rhythm_keys(e - s, _shot_words(analysis, s, e), zoom_index)
        shots.append(PlannedShot(idx, s, e, framing_keys=keys))
    # Every boundary is a hard cut — the framing changes carry the visual
    # change; transitions would just smear it.
    per_cut: list[tuple[str, float] | None] = [("cut", 0.04)] * max(0, len(shots) - 1)
    notes = []
    if len(shots) > len(scene_bounds):
        notes.append(
            f"jump cuts removed dead air: {len(scene_bounds)} shot(s) -> {len(shots)}"
        )
    return EditPlan(
        style="talking_head",
        shots=shots,
        per_cut=per_cut,
        # Restrained 1-3 word captions in the safe lower band (~60-65% down),
        # not karaoke centred on the speaker's face.
        caption_mode="punch",
        caption_position="lower_third",
        notes=notes,
    )


def cinematic_cuts(n_shots: int) -> list[tuple[str, float] | None]:
    """0.8s dissolves; the last cut dips to black (the ending beat) when
    there are at least three shots to earn it. Shared with mixes/planner."""
    n_cuts = max(0, n_shots - 1)
    cuts: list[tuple[str, float] | None] = [("dissolve", 0.8)] * n_cuts
    if n_cuts >= 2:
        cuts[-1] = ("fadeblack", 0.8)
    return cuts


def _plan_cinematic(scene_bounds: list[tuple[int, float, float]]) -> EditPlan:
    """Long dissolves, one dip to black before the final shot; every shot
    gets the (eased, direction-rotating) Ken Burns drift; static lower-third
    captions. A dip on every other cut read as a slideshow."""
    shots = [
        PlannedShot(idx, s, e, force_ken_burns=True) for idx, s, e in scene_bounds
    ]
    per_cut: list[tuple[str, float] | None] = cinematic_cuts(len(shots))
    return EditPlan(
        style="cinematic",
        shots=shots,
        per_cut=per_cut,
        caption_mode="static",
        caption_position="lower_third",
    )


def _plan_chill(scene_bounds: list[tuple[int, float, float]]) -> EditPlan:
    """Gentle long fades, low cut density, unobtrusive captions."""
    shots = [PlannedShot(idx, s, e) for idx, s, e in scene_bounds]
    per_cut: list[tuple[str, float] | None] = [("fade", 0.6)] * max(0, len(shots) - 1)
    return EditPlan(
        style="chill",
        shots=shots,
        per_cut=per_cut,
        caption_mode="static",
        caption_position="lower_third",
    )


def _with_jump_cuts(
    plan: EditPlan, analysis: AnalysisReport, envelope: "SpeechEnvelope | None" = None
) -> EditPlan:
    from reelforge_core.compose.jumpcuts import JUMP_CUT, split_on_silences

    shots: list[PlannedShot] = []
    per_cut: list[tuple[str, float] | None] = []
    for k, shot in enumerate(plan.shots):
        pieces = split_on_silences(
            (shot.in_ts, shot.out_ts), analysis.transcript, envelope=envelope
        )
        for j, (ps, pe) in enumerate(pieces):
            if shots:
                per_cut.append(
                    JUMP_CUT if j > 0 else (plan.per_cut[k - 1] if k > 0 else None)
                )
            shots.append(
                PlannedShot(
                    shot.scene_index,
                    ps,
                    pe,
                    speed=shot.speed,
                    punch_in=shot.punch_in,
                    punch_in_animated=shot.punch_in_animated,
                    force_ken_burns=shot.force_ken_burns,
                )
            )
    return EditPlan(
        style=plan.style,
        shots=shots,
        per_cut=per_cut,
        caption_mode=plan.caption_mode,
        caption_position=plan.caption_position,
        notes=plan.notes + (["forced jump cuts applied"] if len(shots) > len(plan.shots) else []),
    )
