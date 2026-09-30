"""Whole-track music analysis + section choice (pro-editing CP10).

The reel's music used to start at 0:00 and loop blindly: a fade-in over the
song's intro, the drop landing wherever it landed, a hard jump back to the
top when a long reel outran the track. An editor instead picks WHERE in the
song the reel sits. This module gives compose what it needs to do that:

- `analyze_track`: tempo + beat phase (compose/beats.py's estimator run over
  the whole track), the downbeat (which beat of the bar is "one"), 8-bar
  phrases, per-phrase energy and the drop — the phrase boundary with the
  biggest energy jump. Cached per track file (`/data/music/analysis/`).
- `choose_section`: the track offset for a reel: the drop on the payoff when
  there is one to hit, else an offset that ENDS the reel on a phrase
  boundary. Offsets are whole beats from the track's own beat grid, so the
  shifted grid (`grid_for_offset`) keeps every beat-snapped cut on a beat.
- `loop_plan`: for reels longer than what's left of the track, the phrase-
  aligned segments to crossfade instead of a blind stream loop.

Pure except `analyze_track`'s decode + cache.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from reelforge_core.compose.beats import BeatGrid

log = logging.getLogger(__name__)

MUSIC_ANALYSIS_VERSION = "m2"  # m2: every drop, strongest first
BEATS_PER_BAR = 4
BARS_PER_PHRASE = 8
MAX_ANALYZE_SEC = 600.0
END_FADE_SEC = 1.5
LOOP_XFADE_SEC = 2.0
DROP_MIN_RISE = 1.25  # a drop: the next phrase is >= 25% louder (RMS)
HYPE_BPM = (110.0, 150.0)


@dataclass
class TrackAnalysis:
    bpm: float
    phase_sec: float  # first beat
    downbeat_sec: float  # first "one" of a bar
    duration_sec: float
    phrase_sec: float  # one 8-bar phrase
    phrase_starts: list[float]
    phrase_energy: list[float]  # RMS per phrase, normalised to max 1
    drop_sec: float | None
    # Every qualifying rise, strongest first (drop_sec is the first): a drop
    # early in the song can't sit under a payoff 30s into the reel, a
    # later, smaller one can.
    drops: list[float] = field(default_factory=list)
    version: str = MUSIC_ANALYSIS_VERSION

    @property
    def interval(self) -> float:
        return 60.0 / self.bpm

    @property
    def grid(self) -> BeatGrid:
        return BeatGrid(bpm=self.bpm, phase_sec=self.phase_sec)


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------


def _cache_dir() -> Path:
    return Path(os.environ.get("REELFORGE_MUSIC_DIR", "/data/music")) / "analysis"


def analyze_samples(samples, sample_rate: int, bpm: float, phase: float) -> TrackAnalysis:
    """Downbeat, phrases, energy and drop from mono samples + a beat grid.
    Pure given its inputs."""
    import numpy as np

    duration = samples.size / float(sample_rate)
    interval = 60.0 / bpm

    def rms(a: float, b: float) -> float:
        lo, hi = int(max(0.0, a) * sample_rate), int(min(duration, b) * sample_rate)
        if hi <= lo:
            return 0.0
        seg = samples[lo:hi].astype(np.float64)
        return float(math.sqrt(float((seg * seg).mean())))

    # Downbeat: of the four beat positions in a bar, the one whose onsets
    # carry the most energy is "one".
    beats = [phase + k * interval for k in range(int((duration - phase) / interval))]
    accents = [0.0] * BEATS_PER_BAR
    for k, t in enumerate(beats):
        accents[k % BEATS_PER_BAR] += rms(t, t + min(0.1, interval / 3))
    one = int(np.argmax(np.asarray(accents))) if beats else 0
    downbeat = phase + one * interval

    phrase = interval * BEATS_PER_BAR * BARS_PER_PHRASE
    starts: list[float] = []
    t = downbeat
    while t < duration - 1e-6:
        starts.append(round(t, 3))
        t += phrase
    energy = [rms(s, s + phrase) for s in starts]
    peak = max(energy) if energy else 0.0
    energy = [round(e / peak, 4) if peak > 0 else 0.0 for e in energy]
    rises: list[tuple[float, float]] = []  # (rise, phrase start)
    for i in range(1, len(starts)):
        prev = energy[i - 1]
        if prev <= 0:
            continue
        rise = energy[i] / prev
        if rise >= DROP_MIN_RISE and energy[i] >= 0.6:
            rises.append((rise, starts[i]))
    drops = [t for _, t in sorted(rises, key=lambda r: (-r[0], r[1]))]
    drop = drops[0] if drops else None
    return TrackAnalysis(
        bpm=round(bpm, 2), phase_sec=round(phase, 4), downbeat_sec=round(downbeat, 4),
        duration_sec=round(duration, 3), phrase_sec=round(phrase, 4),
        phrase_starts=starts, phrase_energy=energy, drop_sec=drop, drops=drops,
    )


def analyze_track(path: Path) -> TrackAnalysis | None:
    """Analyse (or load the cached analysis of) a music file. None when it
    has no measurable beat."""
    from reelforge_core.compose.beats import SAMPLE_RATE, _decode_mono, detect_beats

    try:
        st = path.stat()
    except OSError:
        return None
    key = f"{path.stem}-{int(st.st_mtime)}-{st.st_size}-{MUSIC_ANALYSIS_VERSION}"
    cache = _cache_dir() / f"{key}.json"
    try:
        return TrackAnalysis(**json.loads(cache.read_text()))
    except (OSError, ValueError, TypeError):
        pass
    grid = detect_beats(path, analyze_sec=MAX_ANALYZE_SEC)
    if grid is None:
        return None
    try:
        samples = _decode_mono(path, MAX_ANALYZE_SEC)
    except Exception:  # noqa: BLE001
        return None
    result = analyze_samples(samples, SAMPLE_RATE, grid.bpm, grid.phase_sec)
    try:
        from reelforge_core.io_utils import write_json_atomic

        cache.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(cache, asdict(result))
    except OSError:  # pragma: no cover — read-only music dir: just don't cache
        pass
    return result


# --------------------------------------------------------------------------
# choices (pure)
# --------------------------------------------------------------------------


def bpm_fits(bpm: float | None, band: tuple[float, float] = HYPE_BPM) -> bool:
    """In the band at its own tempo, double time or half time."""
    if not bpm:
        return False
    return any(band[0] <= bpm * f <= band[1] for f in (1.0, 2.0, 0.5))


def _snap_to_beat(t: float, ta: TrackAnalysis) -> float:
    k = round((t - ta.phase_sec) / ta.interval)
    return ta.phase_sec + k * ta.interval


def choose_section(
    ta: TrackAnalysis | None,
    reel_sec: float,
    drop_at: float | None = None,
) -> tuple[float, str]:
    """(track offset, why). The drop on `drop_at` (mezzanine seconds) when
    the track has one and the reel fits after it; otherwise end the reel on
    a phrase boundary, starting no later than the track's last third;
    otherwise the top of the track. Offsets are whole beats."""
    if ta is None:
        return 0.0, "no beat analysis"
    if drop_at is not None:
        for drop in ta.drops or ([ta.drop_sec] if ta.drop_sec is not None else []):
            off = _snap_to_beat(drop - drop_at, ta)
            if off >= 0.0 and off + reel_sec <= ta.duration_sec:
                return round(off, 3), f"drop at {drop:.1f}s on the payoff"
    latest = ta.duration_sec * 2.0 / 3.0
    for p in [*ta.phrase_starts[1:], ta.duration_sec]:
        off = p - reel_sec
        if 0.0 <= off <= latest and off + reel_sec <= ta.duration_sec:
            snapped = _snap_to_beat(off, ta)
            if snapped >= 0.0:
                return round(snapped, 3), f"ends on the phrase at {p:.1f}s"
    # From the first beat, not 0:00: every offset is a whole beat, so the
    # mezzanine grid is phase 0 at `ta.bpm` whatever section compose ends up
    # choosing — which lets a mix plan its cuts before compose picks one.
    return round(ta.phase_sec % ta.interval, 3), "top of the track"


def mezzanine_grid(ta: TrackAnalysis) -> BeatGrid:
    """The beat grid on the mezzanine clock for ANY section this module
    chooses (all offsets are whole beats). Pure."""
    return BeatGrid(bpm=ta.bpm, phase_sec=0.0)


def grid_for_offset(ta: TrackAnalysis, offset: float) -> BeatGrid:
    """The beat grid on the MEZZANINE clock when the track starts at
    `offset`. Pure."""
    phase = (ta.phase_sec - offset) % ta.interval
    return BeatGrid(bpm=ta.bpm, phase_sec=round(phase, 4))


def refine_offset(ta: TrackAnalysis, offset: float, planned_at: float, actual_at: float) -> float:
    """Move the drop from where the plan estimated the payoff to where it
    actually rendered — by WHOLE beats only, so the grid the cuts were
    snapped to stays valid. Pure."""
    beats = round((actual_at - planned_at) / ta.interval)
    new = offset - beats * ta.interval
    return round(new, 3) if new >= 0.0 else offset


def realign_end(ta: TrackAnalysis, offset: float, total: float, max_beats: int = 8) -> float:
    """Shift the offset by whole beats (<= max_beats) so the reel's ACTUAL
    length ends nearest a phrase boundary — the pre-plan estimate misses by
    whatever ramps, trims and the cold open changed. Whole beats keep the
    cut grid valid. Pure."""
    bounds = [*ta.phrase_starts, ta.duration_sec]
    best, best_err = offset, min(abs(offset + total - p) for p in bounds)
    for k in range(-max_beats, max_beats + 1):
        cand = offset + k * ta.interval
        if cand < 0.0 or cand + total > ta.duration_sec:
            continue
        err = min(abs(cand + total - p) for p in bounds)
        if err < best_err - 1e-6:
            best, best_err = cand, err
    return round(best, 3)


def loop_plan(ta: TrackAnalysis | None, offset: float, total: float) -> list[tuple[float, float]]:
    """Track segments (start, end) that fill `total` seconds: the first from
    `offset`, then phrase-aligned loops from the second phrase to the last
    whole phrase, each joined by a LOOP_XFADE_SEC crossfade. A single
    segment when the track is long enough. Pure."""
    if ta is None or offset + total <= ta.duration_sec or len(ta.phrase_starts) < 3:
        return [(offset, offset + total)]
    loop_start = ta.phrase_starts[1]
    loop_end = ta.phrase_starts[-1]
    if loop_end - loop_start < 4 * LOOP_XFADE_SEC:
        return [(offset, offset + total)]
    # Play from the offset to the last whole phrase, then loop phrases.
    first_end = loop_end if loop_end - offset >= 2 * LOOP_XFADE_SEC else ta.duration_sec
    segs = [(offset, first_end)]
    have = first_end - offset
    while have < total:
        need = total - have + LOOP_XFADE_SEC
        end = min(loop_end, loop_start + need)
        segs.append((loop_start, end))
        have += (end - loop_start) - LOOP_XFADE_SEC
        if len(segs) > 60:
            break
    return [(round(a, 3), round(b, 3)) for a, b in segs]


BED_SWITCH_FRACTION = 0.7  # move to the next track once this much of one is used
BED_MAX_TRACKS = 4
BED_MIN_SEC = 150.0


def chapter_bed(
    tracks: list[tuple[str, TrackAnalysis]],
    chapter_starts: list[float],
    total: float,
) -> list[tuple[float, float, str]]:
    """A long-form bed that changes track at chapter boundaries: stay on a
    track until BED_SWITCH_FRACTION of it is used, then switch at the next
    chapter start, cycling through `tracks`. Each group plays its own
    phrase-ending section (or phrase loops); groups are joined by the same
    LOOP_XFADE_SEC crossfades as loops. (start, end, path) per segment. Pure."""
    if not tracks:
        return []
    bounds = sorted({0.0, *[c for c in chapter_starts if 0.0 < c < total]}) + [total]
    groups: list[tuple[float, float]] = []
    start = 0.0
    ti = 0
    for i in range(1, len(bounds)):
        cut = bounds[i]
        used = cut - start
        is_last = i == len(bounds) - 1
        ta = tracks[ti % len(tracks)][1]
        nxt = bounds[i + 1] - start if not is_last else None
        if is_last or (nxt is not None and nxt > ta.duration_sec * BED_SWITCH_FRACTION and used > 0):
            groups.append((start, cut))
            start = cut
            ti += 1
    segs: list[tuple[float, float, str]] = []
    for gi, (a, b) in enumerate(groups):
        path, ta = tracks[gi % len(tracks)]
        length = (b - a) + (LOOP_XFADE_SEC if gi < len(groups) - 1 else 0.0)
        off, _ = choose_section(ta, length)
        for s, e in loop_plan(ta, off, length):
            segs.append((s, e, path))
    return segs
