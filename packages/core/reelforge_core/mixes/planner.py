"""Bake style pacing into a multi-source mix timeline (pure).

Takes the sequenced (asset_id, in, out) shots and produces a full
`ReelTimeline` with speed / punch-ins / per-cut transitions baked onto
`TimelineShot`s — so the editor shows exactly what renders and the timeline
compose path (which deliberately never applies grammars to user edits) needs
no changes.

Multi-source rules, mirroring compose/styles.py grammars:
- hype: action-led cuts (compose/action.py) — no cut through an action
  event, a speed ramp into the strongest impact of the mix, beat-snapped
  filler, alternating framing, 1.5x through lulls; hard cuts throughout.
- talking_head: jump cuts through each moment's dead air, punch-in
  alternation, all hard cuts.
- cinematic: dissolves, one dip to black before the final moment, Ken Burns everywhere.
- chill: gentle long fades. classic: plain cuts at the reel default.

The output always satisfies the editor PUT rules: <= 60 shots (beat
splitting stops early rather than exceed it), every shot >= 0.15s and inside
its source, no "auto" transition kinds.
"""

from __future__ import annotations

from reelforge_core.compose.beats import BeatGrid
from reelforge_core.compose.styles import (
    HYPE_LULL_Z,
    HYPE_ALT_ZOOM,
)
from reelforge_core.models import (
    AnalysisReport,
    ReelTimeline,
    TimelineShot,
    TransitionStyle,
)

MAX_MIX_SHOTS = 300  # mirror of the editor's MAX_TIMELINE_SHOTS


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
    envelopes: dict | None = None,
) -> ReelTimeline:
    """Sequenced shots -> fully styled multi-source ReelTimeline. Pure.
    `envelopes` (asset_id -> SpeechEnvelope) drive talking-head jump cuts."""
    if style == "hype":
        timeline_shots = _plan_hype(shots, analyses, beat_grid)
    elif style == "talking_head":
        timeline_shots = _plan_talking_head(shots, analyses, envelopes)
    elif style == "cinematic":
        timeline_shots = _plan_uniform(shots, palette=[("dissolve", 0.8)], ken_burns=True)
        # The one dip to black goes before the final moment (styles.cinematic_cuts).
        if len(timeline_shots) >= 3:
            penultimate = timeline_shots[-2]
            timeline_shots[-2] = penultimate.model_copy(
                update={"transition_after": TransitionStyle(kind="fadeblack", duration_sec=0.8)}
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
    framing_keys: tuple = (),
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
        framing_keys=[[round(v, 3) for v in k] for k in framing_keys],
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
    """Action-led cuts per moment (compose/action.py, as in scene mode): no
    cut through an event, cut-ins at the motion low before each, a speed
    ramp into the single strongest impact across the mix, beat-snapped
    filler, alternating framing, 1.5x through lulls, hard cuts only."""
    from reelforge_core.compose.action import action_pieces
    from reelforge_core.reels.events import activity_track, detect_events

    z_cache: dict = {}
    per_asset: dict[str, tuple[list, list[float]]] = {}
    for aid, _s, _e in shots:
        if aid not in per_asset:
            a = analyses.get(aid)
            per_asset[aid] = (
                (detect_events(a), activity_track(a)) if a is not None else ([], [])
            )
    # The strongest event across the mix gets the ramp. Strengths are
    # per-asset z-scores — approximate across assets, but the winner is a
    # genuine peak in its own footage either way.
    money = None
    best = float("-inf")
    for aid, s, e in shots:
        for ev in per_asset[aid][0]:
            if s <= ev.peak_sec <= e and ev.strength > best:
                best, money = ev.strength, ev
    built: list[tuple[TimelineShot, tuple[str, float] | None]] = []
    mezz_cursor = 0.0
    prev_zoom = HYPE_ALT_ZOOM  # so the first alternated piece opens wide
    for aid, s, e in shots:
        events, act = per_asset[aid]

        def activity(t: float, _act=act) -> float | None:
            i = int(t)
            return _act[i] if 0 <= i < len(_act) else None

        pieces, zoom_after = action_pieces(
            s, e, events, activity, grid=grid, mezz_start=mezz_cursor,
            money=money if money in events else None, prev_zoom=prev_zoom,
        )
        # Never exceed the editor cap: stop splitting, keep the whole rest.
        if len(built) + len(pieces) > MAX_MIX_SHOTS:
            built.append((_shot(aid, s, e), ("cut", 0.04)))
            mezz_cursor += e - s
            continue
        prev_zoom = zoom_after
        energy = _energy_z_for(analyses, z_cache, aid)
        for p in pieces:
            speed = p.speed
            if p.kind == "fill" and speed == 1.0 and p.end - p.start >= 2.0:
                zs = [z for t, z in energy if p.start <= t <= p.end]
                if zs and sum(zs) / len(zs) < HYPE_LULL_Z:
                    speed = 1.5
            shot = _shot(aid, p.start, p.end, speed=speed, framing_keys=p.keys)
            # A hard cut before every piece and every moment: a slide on every
            # source change made most joins in a mix flashy.
            built.append((shot, ("cut", 0.04)))
            mezz_cursor += shot.duration
    return _finish_transitions(built)


def _plan_talking_head(
    shots: list[tuple[str, float, float]],
    analyses: dict[str, AnalysisReport | None],
    envelopes: dict | None = None,
) -> list[TimelineShot]:
    from reelforge_core.compose.jumpcuts import split_on_silences

    from reelforge_core.compose.styles import _shot_words, rhythm_keys

    built: list[tuple[TimelineShot, tuple[str, float] | None]] = []
    zoom_index = 0
    for aid, s, e in shots:
        a = analyses.get(aid)
        transcript = a.transcript if a is not None else None
        envelope = (envelopes or {}).get(aid)
        pieces = split_on_silences(
            (s, e), transcript, envelope=envelope, trim_edges=envelope is not None
        )
        if len(built) + len(pieces) > MAX_MIX_SHOTS:
            pieces = [(s, e)]
        for ps, pe in pieces:
            if built:
                prev_shot, _ = built[-1]
                built[-1] = (prev_shot, ("cut", 0.04))
            # Same rhythm as a single-clip talking head (styles.rhythm_keys):
            # a new framing at each cut and about every 3s within a shot.
            words = _shot_words(a, ps, pe) if a is not None else []
            keys, zoom_index = rhythm_keys(pe - ps, words, zoom_index)
            built.append((_shot(aid, ps, pe, framing_keys=keys), None))
    return _finish_transitions(built)


# ---------------------------------------------------------------------------
# Long-form retention structure (CP11)
# ---------------------------------------------------------------------------

# Long-form visual rhythm: a live 6-minute "chill" mix held one framing for
# 80s (1.5 changes/min) — long sections, no grammar that moves the picture.
# Gentler than the talking-head rhythm: a change about every 6s, never more
# than 8s static (the long-form QA target), between two close framings.
LONG_RHYTHM = dict(interval=6.0, force=7.0, max_static=8.0, zooms=(1.0, 1.12))
LONG_STATIC_SEC = 8.0

YT_MIN_CHAPTERS = 3
YT_MIN_CHAPTER_SEC = 10.0
CHAPTER_OVERLAY_SEC = 2.0
REHOOK_OVERLAY_SEC = 3.0
REHOOK_AT = 0.45  # of the running time
REHOOK_WINDOW = (0.35, 0.60)


def youtube_chapters(starts: list[tuple[str, float]], total: float) -> list[tuple[str, float]]:
    """Chapters YouTube will accept: the first at 0:00, each >= 10s (a short
    one folds into the one before), at least 3 — otherwise none. Pure."""
    chapters = sorted(((t or "").strip()[:60], max(0.0, s)) for t, s in starts)
    chapters = [(t, s) for t, s in sorted(chapters, key=lambda c: c[1]) if t]
    if not chapters:
        return []
    title0, _ = chapters[0]
    chapters[0] = (title0, 0.0)
    kept: list[tuple[str, float]] = [chapters[0]]
    for title, start in chapters[1:]:
        if start - kept[-1][1] >= YT_MIN_CHAPTER_SEC:
            kept.append((title, start))
    if len(kept) > 1 and total - kept[-1][1] < YT_MIN_CHAPTER_SEC:
        kept.pop()
    return kept if len(kept) >= YT_MIN_CHAPTERS else []


def format_chapters(chapters: list[tuple[str, float]]) -> str:
    """YouTube description chapter lines: '0:00 Title'. Pure."""
    lines = []
    for title, start in chapters:
        s = int(start)
        stamp = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
        lines.append(f"{stamp} {title}")
    return "\n".join(lines)


def plan_long_form(
    seq,
    analyses: dict[str, AnalysisReport | None],
    style: str,
    beat_grid: BeatGrid | None,
    envelopes: dict | None = None,
) -> ReelTimeline:
    """A long-form mix: the intro montage (seq.intro) cold-opens, a hard cut
    into the story, chapters mapped to the first shot of their section, a
    2s title card at each chapter after the first, and a 'coming up'
    re-hook near the midpoint. Overlays are ordinary timeline overlays —
    the editor shows and can delete them. Pure."""
    from reelforge_core.broll.suggest import shot_segments
    from reelforge_core.models import Chapter, TextOverlay

    from reelforge_core.compose.styles import _shot_words, rhythm_keys

    intro = list(seq.intro)
    items = intro + list(seq.shots)
    tl = plan_mix(items, analyses, style, beat_grid, envelopes)
    shots = list(tl.shots)
    if style != "hype":
        # Keep long shots moving (framing keys never change a duration).
        zoom_index = 0
        for i, sh in enumerate(shots):
            if sh.kind != "video" or sh.framing_keys or sh.duration <= LONG_STATIC_SEC:
                continue
            a = analyses.get(sh.asset_id)
            words = _shot_words(a, sh.in_ts, sh.out_ts, sh.speed or 1.0) if a is not None else []
            keys, zoom_index = rhythm_keys(sh.duration, words, zoom_index, cx=0.5, cy=0.5, **LONG_RHYTHM)
            shots[i] = sh.model_copy(update={"framing_keys": [list(k) for k in keys]})

    # First shot of every item: grammars split items (jump cuts, action
    # cuts) but keep them in order and inside their source range.
    first: list[int | None] = []
    cursor = 0
    for aid, s, e in items:
        j = next(
            (j for j in range(cursor, len(shots))
             if shots[j].asset_id == aid and s - 0.7 <= shots[j].in_ts <= e),
            None,
        )
        first.append(j)
        if j is not None:
            cursor = j + 1

    story_start = first[len(intro)] if len(intro) < len(first) else None
    if intro and story_start:
        # Out of the montage into the story: a hard cut, whatever the grammar.
        k = story_start - 1
        shots[k] = shots[k].model_copy(
            update={"transition_after": TransitionStyle(kind="cut", duration_sec=0.04)}
        )
    tl = tl.model_copy(update={"shots": shots})

    segs = shot_segments(tl)
    total = segs[-1][2] if segs else 0.0

    def at(shot_index: int) -> float:
        return segs[shot_index][1] if 0 <= shot_index < len(segs) else 0.0

    raw: list[tuple[str, int]] = []
    if intro:
        raw.append(("Intro", 0))
    for k, title in enumerate(getattr(seq, "chapter_titles", []) or []):
        j = first[len(intro) + k] if len(intro) + k < len(first) else None
        if title and j is not None:
            raw.append((title, j))
    if raw and raw[0][1] != 0:
        raw[0] = (raw[0][0], 0)
    valid = youtube_chapters([(t, at(j)) for t, j in raw], total)
    by_time = {round(at(j), 3): (t, j) for t, j in raw}
    chapters = [Chapter(title=t, shot_index=by_time.get(round(s, 3), (t, 0))[1]) for t, s in valid]

    overlays: list[TextOverlay] = []
    for i, (title, start) in enumerate(valid):
        if i == 0:
            continue
        overlays.append(TextOverlay(
            id=f"chapter-{i + 1}", text=title, start_sec=round(start + 0.3, 3),
            end_sec=round(start + 0.3 + CHAPTER_OVERLAY_SEC, 3), position="top",
            font_size_px=72,
        ))
    rehook = getattr(seq, "rehook_text", None)
    if rehook and total > 60.0:
        lo, hi = REHOOK_WINDOW[0] * total, REHOOK_WINDOW[1] * total
        boundaries = [start for _, start in valid if lo <= start <= hi] or [
            a for _, a, _ in segs if lo <= a <= hi
        ]
        t = min(boundaries, key=lambda b: abs(b - REHOOK_AT * total)) if boundaries else REHOOK_AT * total
        if any(abs(o.start_sec - t) < CHAPTER_OVERLAY_SEC + 0.5 for o in overlays):
            t += CHAPTER_OVERLAY_SEC + 0.5  # after the chapter card, not on it
        overlays.append(TextOverlay(
            id="rehook", text=f"Coming up: {rehook}", start_sec=round(t, 3),
            end_sec=round(t + REHOOK_OVERLAY_SEC, 3), position="top", font_size_px=64,
        ))
    return tl.model_copy(update={"overlays": overlays, "chapters": chapters})
