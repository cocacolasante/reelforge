"""Bake style pacing into a multi-source mix timeline (pure).

Takes the sequenced (asset_id, in, out) shots and produces a full
`ReelTimeline` with speed / punch-ins / per-cut transitions baked onto
`TimelineShot`s — so the editor shows exactly what renders and the timeline
compose path (which deliberately never applies grammars to user edits) needs
no changes.

Multi-source rules, mirroring compose/styles.py grammars:
- hype: beat-split long moments (styles._beat_pieces), slow-mo + drifting
  punch-in on the single biggest energy peak (per-asset z-scores), 1.5x
  through lulls; hard cuts within a moment; between moments, a source-video
  change gets a quick slide (reads as a camera change), same-source cuts
  stay hard.
- talking_head: jump cuts through each moment's dead air, punch-in
  alternation, all hard cuts.
- cinematic: dissolve / dip-to-black alternation, Ken Burns everywhere.
- chill: gentle long fades. classic: plain cuts at the reel default.

The output always satisfies the editor PUT rules: <= 60 shots (beat
splitting stops early rather than exceed it), every shot >= 0.15s and inside
its source, no "auto" transition kinds.
"""

from __future__ import annotations

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.compose.styles import (
    HYPE_LULL_Z,
    HYPE_MAX_SHOT_SEC,
    HYPE_SLOWMO_MAX_SRC_SEC,
    TH_PUNCH_IN,
    _beat_pieces,
)
from reelforge_core.models import (
    AnalysisReport,
    ReelTimeline,
    TimelineShot,
    TransitionStyle,
)

MAX_MIX_SHOTS = 60  # mirror of the editor's MAX_TIMELINE_SHOTS


def _energy_z_for(
    analyses: dict[str, AnalysisReport | None], cache: dict, asset_id: str
) -> list[tuple[float, float]]:
    if asset_id not in cache:
        a = analyses.get(asset_id)
        if a is None or not a.energy:
            cache[asset_id] = []
        else:
            from reelforge_core.reels.generators.moment import combined_scores

            cache[asset_id] = combined_scores(a)
    return cache[asset_id]


def plan_mix(
    shots: list[tuple[str, float, float]],
    analyses: dict[str, AnalysisReport | None],
    style: str,
    beat_grid: BeatGrid | None,
) -> ReelTimeline:
    """Sequenced shots -> fully styled multi-source ReelTimeline. Pure."""
    if style == "hype":
        timeline_shots = _plan_hype(shots, analyses, beat_grid)
    elif style == "talking_head":
        timeline_shots = _plan_talking_head(shots, analyses)
    elif style == "cinematic":
        timeline_shots = _plan_uniform(
            shots, palette=[("dissolve", 0.8), ("fadeblack", 0.8)], ken_burns=True
        )
    elif style == "chill":
        timeline_shots = _plan_uniform(shots, palette=[("fade", 0.6)], ken_burns=False)
    else:  # classic
        timeline_shots = _plan_uniform(shots, palette=[("cut", 0.04)], ken_burns=False)
    return ReelTimeline(shots=timeline_shots[:MAX_MIX_SHOTS])


def _shot(
    asset_id: str,
    in_ts: float,
    out_ts: float,
    *,
    speed: float = 1.0,
    punch_in: float | None = None,
    punch_in_animated: bool = False,
    ken_burns: bool = False,
    transition: tuple[str, float] | None = None,
) -> TimelineShot:
    return TimelineShot(
        kind="video",
        asset_id=asset_id,
        in_ts=round(max(0.0, in_ts), 3),
        out_ts=round(out_ts, 3),
        ken_burns=ken_burns,
        speed=speed,
        punch_in=punch_in,
        punch_in_animated=punch_in_animated,
        transition_after=(
            TransitionStyle(kind=transition[0], duration_sec=transition[1])  # type: ignore[arg-type]
            if transition is not None
            else None
        ),
    )


def _finish_transitions(
    built: list[tuple[TimelineShot, tuple[str, float] | None]]
) -> list[TimelineShot]:
    """Attach each boundary's transition to the PRECEDING shot; the last shot
    carries none."""
    out: list[TimelineShot] = []
    for i, (shot, boundary_after) in enumerate(built):
        if i == len(built) - 1 or boundary_after is None:
            out.append(shot)
        else:
            out.append(
                shot.model_copy(
                    update={
                        "transition_after": TransitionStyle(
                            kind=boundary_after[0], duration_sec=boundary_after[1]
                        )
                    }
                )
            )
    return out


def _plan_uniform(
    shots: list[tuple[str, float, float]],
    *,
    palette: list[tuple[str, float]],
    ken_burns: bool,
) -> list[TimelineShot]:
    built: list[tuple[TimelineShot, tuple[str, float] | None]] = []
    for k, (aid, s, e) in enumerate(shots):
        built.append(
            (
                _shot(aid, s, e, ken_burns=ken_burns),
                palette[k % len(palette)],
            )
        )
    return _finish_transitions(built)


def _plan_hype(
    shots: list[tuple[str, float, float]],
    analyses: dict[str, AnalysisReport | None],
    grid: BeatGrid | None,
) -> list[TimelineShot]:
    z_cache: dict = {}

    # The single biggest moment across the mix gets the slow-mo. z-scores are
    # per-asset normalized — cross-asset comparison is approximate but the
    # winner is a genuine peak in its own footage either way.
    peak: tuple[str, float] | None = None  # (asset_id, time)
    best_z = float("-inf")
    for aid, s, e in shots:
        for t, z in _energy_z_for(analyses, z_cache, aid):
            if s <= t <= e and z > best_z:
                best_z = z
                peak = (aid, t)

    built: list[tuple[TimelineShot, tuple[str, float] | None]] = []
    mezz_cursor = 0.0
    slide = ["slideleft", "slideright"]
    source_change_count = 0
    prev_aid: str | None = None
    for aid, s, e in shots:
        # Boundary transition BEFORE this moment: source change reads as a
        # camera change -> quick slide; same source -> hard cut.
        if built:
            if aid != prev_aid:
                boundary = (slide[source_change_count % 2], 0.25)
                source_change_count += 1
            else:
                boundary = ("cut", 0.04)
            prev_shot, _ = built[-1]
            built[-1] = (prev_shot, boundary)
        prev_aid = aid

        pieces = (
            _beat_pieces(s, e, mezz_cursor, grid)
            if e - s > HYPE_MAX_SHOT_SEC
            else [(s, e)]
        )
        # Never exceed the editor cap: stop splitting, keep the whole rest.
        if len(built) + len(pieces) > MAX_MIX_SHOTS:
            pieces = [(s, e)]
        energy = _energy_z_for(analyses, z_cache, aid)
        for pi, (ps, pe) in enumerate(pieces):
            if pi > 0:
                prev_shot, _ = built[-1]
                built[-1] = (prev_shot, ("cut", 0.04))
            speed = 1.0
            punch = None
            animated = False
            src_dur = pe - ps
            if (
                peak is not None
                and peak[0] == aid
                and ps <= peak[1] <= pe
                and src_dur <= HYPE_SLOWMO_MAX_SRC_SEC
            ):
                speed = 0.5
                punch = 1.2
                animated = True
                peak = None
            else:
                zs = [z for t, z in energy if ps <= t <= pe]
                if zs and sum(zs) / len(zs) < HYPE_LULL_Z and src_dur >= 2.0:
                    speed = 1.5
            shot = _shot(
                aid, ps, pe, speed=speed, punch_in=punch, punch_in_animated=animated
            )
            built.append((shot, None))
            mezz_cursor += shot.duration
    return _finish_transitions(built)


def _plan_talking_head(
    shots: list[tuple[str, float, float]],
    analyses: dict[str, AnalysisReport | None],
) -> list[TimelineShot]:
    from reelforge_core.compose.jumpcuts import split_on_silences

    built: list[tuple[TimelineShot, tuple[str, float] | None]] = []
    k = 0
    for aid, s, e in shots:
        a = analyses.get(aid)
        transcript = a.transcript if a is not None else None
        pieces = split_on_silences((s, e), transcript)
        if len(built) + len(pieces) > MAX_MIX_SHOTS:
            pieces = [(s, e)]
        for ps, pe in pieces:
            if built:
                prev_shot, _ = built[-1]
                built[-1] = (prev_shot, ("cut", 0.04))
            built.append(
                (
                    _shot(aid, ps, pe, punch_in=TH_PUNCH_IN if k % 2 == 1 else None),
                    None,
                )
            )
            k += 1
    return _finish_transitions(built)
