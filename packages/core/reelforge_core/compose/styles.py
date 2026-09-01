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

from dataclasses import dataclass, field

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.models import AnalysisReport, ComposeConfig, RankedReel

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
HYPE_TARGET_SHOT_SEC = 2.6
HYPE_MAX_SHOT_SEC = 4.0
HYPE_SLOWMO_MAX_SRC_SEC = 3.0  # only slow-mo a peak piece this short
HYPE_LULL_Z = -0.2

# talking-head punch-in
TH_PUNCH_IN = 1.25

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
) -> EditPlan:
    """Rewrite the base shot plan per the style grammar. Pure, deterministic."""
    if style == "hype":
        plan = _plan_hype(scene_bounds, analysis, beat_grid)
    elif style == "talking_head":
        plan = _plan_talking_head(scene_bounds, analysis)
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
        plan = _with_jump_cuts(plan, analysis)
    return plan


# ---------------------------------------------------------------------------
# grammars
# ---------------------------------------------------------------------------


def _plan_hype(
    scene_bounds: list[tuple[int, float, float]],
    analysis: AnalysisReport,
    grid: BeatGrid | None,
) -> EditPlan:
    """Fast beat-placed cuts; slow-mo into the biggest energy peak; speed
    through lulls; hard cuts inside shots, quick slides between scenes."""
    energy = _energy_z(analysis)
    shots: list[PlannedShot] = []
    per_cut: list[tuple[str, float] | None] = []
    notes: list[str] = []
    mezz_cursor = 0.0
    slide = ["slideleft", "slideright"]
    scene_cut_count = 0

    # The single biggest moment in the whole reel gets the slow-mo.
    peak_t: float | None = None
    if energy:
        span_pts = [
            (t, z)
            for t, z in energy
            if any(s <= t <= e for _, s, e in scene_bounds)
        ]
        if span_pts:
            peak_t = max(span_pts, key=lambda p: p[1])[0]

    for bi, (idx, s, e) in enumerate(scene_bounds):
        pieces = _beat_pieces(s, e, mezz_cursor, grid)
        for pi, (ps, pe) in enumerate(pieces):
            speed = 1.0
            punch: float | None = None
            animated = False
            src_dur = pe - ps
            if peak_t is not None and ps <= peak_t <= pe and src_dur <= HYPE_SLOWMO_MAX_SRC_SEC:
                speed = 0.5
                punch = 1.2
                animated = True
                notes.append(f"slow-mo on the energy peak at {peak_t:.1f}s")
                peak_t = None  # one money shot only
            else:
                zs = [z for t, z in energy if ps <= t <= pe]
                if zs and sum(zs) / len(zs) < HYPE_LULL_Z and src_dur >= 2.0:
                    speed = 1.5
            shot = PlannedShot(idx, ps, pe, speed=speed, punch_in=punch, punch_in_animated=animated)
            if shots:
                if pi > 0:
                    per_cut.append(("cut", 0.04))
                else:
                    per_cut.append((slide[scene_cut_count % 2], 0.25))
                    scene_cut_count += 1
            shots.append(shot)
            mezz_cursor += shot.duration
    if len(shots) > len(scene_bounds):
        notes.append(f"beat-placed cuts: {len(scene_bounds)} shot(s) -> {len(shots)}")
    return EditPlan(style="hype", shots=shots, per_cut=per_cut, notes=notes)


def _beat_pieces(
    s: float, e: float, mezz_start: float, grid: BeatGrid | None
) -> list[tuple[float, float]]:
    """Split [s, e] into ~HYPE_TARGET_SHOT_SEC pieces whose mezzanine cut
    points land on beats when a grid exists."""
    if e - s <= HYPE_MAX_SHOT_SEC:
        return [(s, e)]
    pieces: list[tuple[float, float]] = []
    cur = s
    cursor = mezz_start
    while e - cur > HYPE_MAX_SHOT_SEC:
        target_mezz = cursor + HYPE_TARGET_SHOT_SEC
        cut_mezz = grid.snap(target_mezz) if grid is not None else target_mezz
        length = cut_mezz - cursor
        if length < 1.0:
            length = (
                cut_mezz + grid.interval - cursor if grid is not None else 1.0
            )
        if e - (cur + length) < 1.0:
            break  # leave a healthy final piece
        pieces.append((cur, round(cur + length, 3)))
        cur = round(cur + length, 3)
        cursor += length
    pieces.append((cur, e))
    return pieces


def _plan_talking_head(
    scene_bounds: list[tuple[int, float, float]],
    analysis: AnalysisReport,
) -> EditPlan:
    """Jump-cut the dead air; alternate punch-ins instead of transitions;
    karaoke captions front and center."""
    from reelforge_core.compose.jumpcuts import apply_jump_cuts

    shots_raw, cuts_raw = apply_jump_cuts(scene_bounds, analysis.transcript)
    shots = [
        PlannedShot(
            idx,
            s,
            e,
            punch_in=TH_PUNCH_IN if k % 2 == 1 else None,
        )
        for k, (idx, s, e) in enumerate(shots_raw)
    ]
    # Every boundary is a hard cut — the punch-in alternation carries the
    # visual change, transitions would just smear it.
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
        caption_mode="karaoke",
        caption_position="centered",
        notes=notes,
    )


def _plan_cinematic(scene_bounds: list[tuple[int, float, float]]) -> EditPlan:
    """Long dissolves alternating with dips to black; every shot gets the
    (eased, direction-rotating) Ken Burns drift; static lower-third captions."""
    shots = [
        PlannedShot(idx, s, e, force_ken_burns=True) for idx, s, e in scene_bounds
    ]
    palette = [("dissolve", 0.8), ("fadeblack", 0.8)]
    per_cut: list[tuple[str, float] | None] = [
        palette[i % 2] for i in range(max(0, len(shots) - 1))
    ]
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


def _with_jump_cuts(plan: EditPlan, analysis: AnalysisReport) -> EditPlan:
    from reelforge_core.compose.jumpcuts import JUMP_CUT, split_on_silences

    shots: list[PlannedShot] = []
    per_cut: list[tuple[str, float] | None] = []
    for k, shot in enumerate(plan.shots):
        pieces = split_on_silences((shot.in_ts, shot.out_ts), analysis.transcript)
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
