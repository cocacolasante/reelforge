"""AI B-roll suggestions: where to cut away while the speaker talks.

One forced tool-use call sees the reel's spoken lines on the mezzanine
timeline plus a catalog of B-roll candidates — scenes from the project's
footage (thumbnail + AI scene summary/tags) and photos (thumbnail) — and
proposes picture-layer placements that illustrate what's being said. The
model's output is never trusted: `validate_suggestions` drops unknown
candidates, clamps lengths and source offsets, and rejects overlaps with
each other and with existing B-roll. Nothing is applied here — the editor
shows each suggestion for accept/reject.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reelforge_core.models import (
    AnalysisReport,
    PictureLayer,
    ReelTimeline,
    Transcript,
    UsageTotals,
)

log = logging.getLogger(__name__)

BROLL_PROMPT_VERSION = "b2"
MAX_CANDIDATES = 40
# Suggestion budget: at least MAX_SUGGESTIONS, about one per
# SECONDS_PER_SUGGESTION of reel, never more than MAX_SUGGESTIONS_CAP — a
# flat 8 left a 5-minute video mostly bare (grantmind breakdown, 2026-09-16).
MAX_SUGGESTIONS = 8
MAX_SUGGESTIONS_CAP = 20
SECONDS_PER_SUGGESTION = 25.0
MIN_LAYER_SEC = 1.5
MAX_LAYER_SEC = 6.0
LONG_REEL_SEC = 120.0
LONG_REEL_MAX_LAYER_SEC = 8.0
MIN_SCENE_SEC = 1.0
# A SPEAKING scene already this much on screen as a main shot is the
# speaker's own footage, not B-roll — its unused stretches are just more
# talking head. Silent footage (screen recordings, cutaways) offers every
# unused stretch however much of it the main cut shows.
MAIN_OVERLAP_FRAC = 0.3
# Cutaways sit at least this far apart, so they land sporadically across the
# reel instead of stacking up where one topic comes up.
MIN_GAP_SEC = 5.0
# The opening hook stays on the speaker — the prompt asks for it and the
# model still placed a cutaway at 0.0s on a long video.
INTRO_HOLD_SEC = 2.0
# Unused stretches offered per scene (the longest first), so a heavily cut
# main track can't flood the catalog with fragments.
MAX_FRAGMENTS_PER_SCENE = 3
# Mirrors the editor preview's buildSegments (reel-default xfade, hard cut).
DEFAULT_XFADE_SEC = 0.4
CUT_SEC = 0.04
LINE_GAP_SEC = 0.6
LINE_MAX_WORDS = 14
MAX_LINES = 200
THUMB_WIDTH = 320

SYSTEM_PROMPT = """You are a video editor adding B-roll to a talking-head reel.

You get the speaker's lines with their times on the reel timeline, the B-roll
already placed, and a catalog of candidates from the same project — stretches
of video the main cut doesn't already show, and photos, each with a thumbnail
(video stretches also have an AI summary and tags).

Propose cutaways that ILLUSTRATE what is being said: put a candidate over the
line it matches, starting as the relevant words begin.

Rules:
- Only use candidate ids from the catalog.
- Each placement lasts {min_len:g}-{max_len:g} seconds; placements never overlap
  each other or existing B-roll, and sit at least {gap:g} seconds apart.
- Keep the speaker on screen for the first 2 seconds and for personal or
  emotional moments.
- Well-matched cutaways beat filling time: at most {budget}, and none at all
  if nothing genuinely matches.
- Spread them across the WHOLE reel — beginning, middle and end — instead of
  clustering where one topic comes up. If the user direction names a clip or
  kind of footage, weave it in throughout wherever it supports what's being
  said, not only where it is mentioned by name.
- Skip candidates that just show the same person talking to camera.
- mode "pip" (picture-in-picture) when the speaker's reaction matters while
  the B-roll plays; otherwise "full".
- For video candidates, source_offset_sec picks where in the scene to start
  (0 = scene start); choose the most relevant moment.
- reason: one sentence. quote: the words the cutaway illustrates."""

RECORD_BROLL = {
    "name": "record_broll",
    "description": "Record the proposed B-roll placements.",
    "input_schema": {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "start_sec": {"type": "number"},
                        "end_sec": {"type": "number"},
                        "source_offset_sec": {"type": "number"},
                        "mode": {"type": "string", "enum": ["full", "pip"]},
                        "quote": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["candidate_id", "start_sec", "end_sec", "reason"],
                },
            }
        },
        "required": ["suggestions"],
    },
}


@dataclass(frozen=True)
class Candidate:
    id: str
    kind: str  # "video" | "photo"
    asset_id: str
    filename: str
    start: float = 0.0  # source seconds (video)
    end: float = 0.0
    summary: str = ""
    tags: tuple[str, ...] = ()
    thumb: Path | None = None

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class SpokenLine:
    start: float  # mezzanine seconds
    end: float
    text: str


@dataclass
class VideoSource:
    asset_id: str
    filename: str
    analysis: AnalysisReport
    working_dir: Path


@dataclass
class PhotoSource:
    asset_id: str
    filename: str
    thumb: Path | None = None


@dataclass
class SuggestResult:
    suggestions: list[dict] = field(default_factory=list)
    usage: UsageTotals = field(default_factory=UsageTotals)
    note: str | None = None


# ---------------------------------------------------------------------------
# timeline mapping (pure)
# ---------------------------------------------------------------------------


def shot_segments(timeline: ReelTimeline) -> list[tuple[Any, float, float]]:
    """(shot, mezzanine start, end) per shot — the editor preview's placement
    math (crossfades overlap neighbouring shots). Pure."""
    out: list[tuple[Any, float, float]] = []
    cursor = 0.0
    n = len(timeline.shots)
    for i, shot in enumerate(timeline.shots):
        dur = shot.duration
        start = cursor
        end = start + dur
        out.append((shot, start, end))
        if i < n - 1:
            tr = shot.transition_after
            x = DEFAULT_XFADE_SEC if tr is None else (CUT_SEC if tr.kind == "cut" else max(CUT_SEC, tr.duration_sec))
            cursor = end - min(x, dur / 2)
    return out


def program_duration(timeline: ReelTimeline) -> float:
    segs = shot_segments(timeline)
    return round(segs[-1][2], 3) if segs else 0.0


def spoken_lines(
    timeline: ReelTimeline, transcripts: dict[str, Transcript | None]
) -> list[SpokenLine]:
    """The main track's words on the mezzanine timeline, grouped into lines at
    sentence ends, pauses and a word cap. Sped-up/slowed shots render muted,
    so they contribute no words. Pure."""
    words: list[tuple[float, float, str]] = []
    for shot, seg_start, seg_end in shot_segments(timeline):
        if shot.kind != "video" or (shot.speed or 1.0) != 1.0:
            continue
        tr = transcripts.get(shot.asset_id)
        if tr is None:
            continue
        for seg in tr.segments:
            for w in seg.words:
                mid = (w.start + w.end) / 2
                if not (shot.in_ts <= mid < shot.out_ts):
                    continue
                ms = seg_start + (w.start - shot.in_ts)
                me = min(seg_end, seg_start + (w.end - shot.in_ts))
                words.append((round(max(seg_start, ms), 2), round(me, 2), w.word.strip()))
    words.sort()
    lines: list[SpokenLine] = []
    cur: list[tuple[float, float, str]] = []
    for w in words:
        if cur and (
            w[0] - cur[-1][1] > LINE_GAP_SEC
            or len(cur) >= LINE_MAX_WORDS
            or cur[-1][2].endswith((".", "?", "!"))
        ):
            lines.append(SpokenLine(cur[0][0], cur[-1][1], " ".join(t for _, _, t in cur)))
            cur = []
        cur.append(w)
    if cur:
        lines.append(SpokenLine(cur[0][0], cur[-1][1], " ".join(t for _, _, t in cur)))
    return lines[:MAX_LINES]


def free_ranges(
    start: float, end: float, used: list[tuple[float, float]], min_len: float
) -> list[tuple[float, float]]:
    """Parts of [start, end] not covered by any `used` range, each at least
    `min_len` long, ascending. Pure."""
    out: list[tuple[float, float]] = []
    cursor = start
    for a, b in sorted(used):
        if b <= cursor or a >= end:
            continue
        if a - cursor >= min_len:
            out.append((round(cursor, 3), round(min(a, end), 3)))
        cursor = max(cursor, b)
        if cursor >= end:
            break
    if end - cursor >= min_len:
        out.append((round(cursor, 3), round(end, 3)))
    return out


def collect_candidates(
    timeline: ReelTimeline,
    videos: list[VideoSource],
    photos: list[PhotoSource],
    max_candidates: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """B-roll catalog: every photo, plus the stretches of each video scene the
    main cut doesn't already show. Dropping whole scenes that were partly on
    screen hid most of a clip that is also a main section — a narrated demo
    used near the end had almost nothing left to cut away to. Balanced
    round-robin across sources under the cap. Pure apart from checking
    thumbnails exist."""
    main_ranges: dict[str, list[tuple[float, float]]] = {}
    for shot in timeline.shots:
        if shot.kind == "video":
            main_ranges.setdefault(shot.asset_id, []).append((shot.in_ts, shot.out_ts))

    pools: list[list[Candidate]] = []
    for p in photos:
        pools.append([Candidate(id="", kind="photo", asset_id=p.asset_id, filename=p.filename, thumb=p.thumb)])
    for v in videos:
        sem = {s.scene_index: s for s in v.analysis.semantics}
        pool: list[Candidate] = []
        for sc in v.analysis.scenes:
            if sc.end_sec - sc.start_sec < MIN_SCENE_SEC:
                continue
            s = sem.get(sc.index)
            used_ranges = main_ranges.get(v.asset_id, [])
            if s is None or s.has_speech:
                used = sum(
                    max(0.0, min(sc.end_sec, b) - max(sc.start_sec, a)) for a, b in used_ranges
                )
                if used / (sc.end_sec - sc.start_sec) > MAIN_OVERLAP_FRAC:
                    continue
            thumb = v.working_dir / sc.thumbnail_path
            fragments = free_ranges(sc.start_sec, sc.end_sec, used_ranges, MIN_LAYER_SEC)
            longest = sorted(fragments, key=lambda r: r[0] - r[1])[:MAX_FRAGMENTS_PER_SCENE]
            for a, b in sorted(longest):
                pool.append(
                    Candidate(
                        id="",
                        kind="video",
                        asset_id=v.asset_id,
                        filename=v.filename,
                        start=a,
                        end=b,
                        summary=s.summary if s else "",
                        tags=tuple(s.tags) if s else (),
                        thumb=thumb if thumb.exists() else None,
                    )
                )
        if pool:
            pools.append(pool)

    picked: list[Candidate] = []
    depth = 0
    while len(picked) < max_candidates and any(depth < len(p) for p in pools):
        for pool in pools:
            if depth < len(pool) and len(picked) < max_candidates:
                picked.append(pool[depth])
        depth += 1
    return [
        Candidate(**{**c.__dict__, "id": f"c{i + 1}"}) for i, c in enumerate(picked)
    ]


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


def _describe(c: Candidate) -> str:
    if c.kind == "photo":
        return f'{c.id} · photo "{c.filename}"'
    tags = f" · tags: {', '.join(c.tags)}" if c.tags else ""
    summary = f" · {c.summary}" if c.summary else ""
    return (
        f'{c.id} · video "{c.filename}" {c.start:.1f}-{c.end:.1f}s '
        f"({c.length:.1f}s long){summary}{tags}"
    )


def build_messages(
    lines: list[SpokenLine],
    candidates: list[Candidate],
    program_sec: float,
    existing: list[tuple[float, float]],
    prompt: str | None = None,
) -> list[dict]:
    blocks: list[dict] = [
        {"type": "text", "text": "B-roll catalog (each candidate: its description, then its thumbnail when available):"}
    ]
    for c in candidates:
        blocks.append({"type": "text", "text": _describe(c)})
        if c.thumb is not None:
            try:
                data = base64.standard_b64encode(c.thumb.read_bytes()).decode()
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}
                )
            except OSError:
                pass
    context: dict[str, Any] = {
        "reel_duration_sec": round(program_sec, 2),
        "max_cutaways": suggestion_budget(program_sec),
        "spoken_lines": [
            {"start_sec": ln.start, "end_sec": ln.end, "text": ln.text} for ln in lines
        ],
        "existing_broll": [{"start_sec": a, "end_sec": b} for a, b in existing],
    }
    if prompt:
        context["user_direction"] = prompt
    blocks.append({"type": "text", "text": json.dumps(context, indent=1)})
    return [{"role": "user", "content": blocks}]


# ---------------------------------------------------------------------------
# validation (pure)
# ---------------------------------------------------------------------------


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > 1e-3


def suggestion_budget(program_sec: float) -> int:
    """How many cutaways a reel of this length may get. Pure."""
    return int(
        min(MAX_SUGGESTIONS_CAP, max(MAX_SUGGESTIONS, math.ceil(program_sec / SECONDS_PER_SUGGESTION)))
    )


def max_layer_sec_for(program_sec: float) -> float:
    """Longest single cutaway: a little longer on long-form reels. Pure."""
    return LONG_REEL_MAX_LAYER_SEC if program_sec > LONG_REEL_SEC else MAX_LAYER_SEC


def _too_close(a: tuple[float, float], b: tuple[float, float], gap: float) -> bool:
    return max(a[0], b[0]) - min(a[1], b[1]) < gap


def validate_suggestions(
    raw: list[Any],
    candidates: list[Candidate],
    program_sec: float,
    existing: list[tuple[float, float]],
    *,
    max_suggestions: int = MAX_SUGGESTIONS,
    max_layer_sec: float = MAX_LAYER_SEC,
    min_gap_sec: float = MIN_GAP_SEC,
    main_track: list[tuple[str, float, float]] = (),  # type: ignore[assignment]
) -> list[dict]:
    """Model output -> safe layer suggestions (PictureLayer fields + reason +
    quote), sorted by start. Anything unusable is dropped, never repaired
    into a different idea. Suggestions never overlap existing B-roll and sit
    at least `min_gap_sec` apart from each other. `main_track` is
    (asset_id, start, end) per main shot: cutting away to the clip that is
    already on screen shows nothing new, so those are dropped too."""
    by_id = {c.id: c for c in candidates}
    taken = list(existing)
    placed: list[tuple[float, float]] = []
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        c = by_id.get(str(item.get("candidate_id", "")))
        if c is None:
            continue
        try:
            start = max(0.0, float(item.get("start_sec", 0.0)))
            end = float(item.get("end_sec", start))
            offset = float(item.get("source_offset_sec") or 0.0)
        except (TypeError, ValueError):
            continue
        dur = min(max(end - start, MIN_LAYER_SEC), max_layer_sec)
        if c.kind == "video":
            if c.length < MIN_LAYER_SEC:
                continue
            dur = min(dur, c.length)
        if start < INTRO_HOLD_SEC and program_sec >= INTRO_HOLD_SEC + dur:
            start = INTRO_HOLD_SEC
        if start + dur > program_sec:
            start = program_sec - dur
        if start < 0 or dur < MIN_SCENE_SEC:
            continue
        window = (round(start, 2), round(start + dur, 2))
        if any(_overlaps(window, t) for t in taken):
            continue
        if any(_too_close(window, p, min_gap_sec) for p in placed):
            continue
        if c.kind == "video" and any(
            aid == c.asset_id and _overlaps(window, (a, b)) for aid, a, b in main_track
        ):
            continue
        in_ts = 0.0
        if c.kind == "video":
            in_ts = round(c.start + min(max(0.0, offset), c.length - dur), 3)
        mode = item.get("mode") if item.get("mode") in ("full", "pip") else "full"
        layer = PictureLayer(
            id=f"sug-{len(out) + 1}",
            kind=c.kind,  # type: ignore[arg-type]
            asset_id=c.asset_id,
            start_sec=window[0],
            end_sec=window[1],
            in_ts=in_ts,
            mode=mode,  # type: ignore[arg-type]
        )
        taken.append(window)
        placed.append(window)
        out.append(
            {
                **layer.model_dump(exclude={"path"}),
                "filename": c.filename,
                "reason": str(item.get("reason", ""))[:300],
                "quote": str(item.get("quote", ""))[:200],
            }
        )
        if len(out) >= max_suggestions:
            break
    return sorted(out, key=lambda s: s["start_sec"])


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def photo_thumbnail(source: Path, working_dir: Path) -> Path | None:
    """A small JPEG of a photo for the model (cached next to the asset's
    working files). None when ffmpeg can't read it."""
    out = working_dir / "broll_thumb.jpg"
    try:
        if out.exists() and out.stat().st_mtime >= source.stat().st_mtime:
            return out
        working_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vf",
             f"scale={THUMB_WIDTH}:-2", "-frames:v", "1", str(out)],
            check=True, capture_output=True, timeout=30,
        )
        return out
    except (OSError, subprocess.SubprocessError):
        return None


def _extract(resp: Any) -> list[Any]:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "record_broll":
            inp = getattr(block, "input", {}) or {}
            return inp.get("suggestions", []) if isinstance(inp, dict) else []
    raise ValueError("no record_broll tool_use block in response")


async def suggest_broll(
    timeline: ReelTimeline,
    videos: list[VideoSource],
    photos: list[PhotoSource],
    transcripts: dict[str, Transcript | None],
    *,
    model: str,
    prompt: str | None = None,
    client: Any | None = None,
) -> SuggestResult:
    """Suggest B-roll for `timeline`. Returns no suggestions (with a note)
    without calling the model when there's nothing to match; model errors
    propagate so the job reports them."""
    from reelforge_core.reels.rank import _accumulate_usage, _call_model

    lines = spoken_lines(timeline, transcripts)
    candidates = collect_candidates(timeline, videos, photos)
    if not candidates:
        return SuggestResult(
            note="No B-roll to choose from — upload other clips or photos to this project first."
        )
    if not lines:
        return SuggestResult(
            note="This reel has no transcribed speech to match B-roll to (is the clip analyzed?)."
        )
    program_sec = program_duration(timeline)
    existing = [(ly.start_sec, ly.end_sec) for ly in timeline.layers]
    budget = suggestion_budget(program_sec)
    max_len = max_layer_sec_for(program_sec)
    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
    resp = await _call_model(
        client,
        model=model,
        temperature=0.0,
        system_prompt=SYSTEM_PROMPT.format(
            min_len=MIN_LAYER_SEC, max_len=max_len, gap=MIN_GAP_SEC, budget=budget
        ),
        messages=build_messages(lines, candidates, program_sec, existing, prompt),
        tools=[RECORD_BROLL],
        tool_name="record_broll",
        max_tokens=6000,
    )
    raw = _extract(resp)
    suggestions = validate_suggestions(
        raw,
        candidates,
        program_sec,
        existing,
        max_suggestions=budget,
        max_layer_sec=max_len,
        main_track=[
            (shot.asset_id, a, b) for shot, a, b in shot_segments(timeline) if shot.kind == "video"
        ],
    )
    note = None if suggestions else "The AI didn't find B-roll that matches what's said."
    log.info(
        "broll: %d candidate(s), %d line(s) -> %d/%d suggestion(s) kept",
        len(candidates), len(lines), len(suggestions), len(raw) if isinstance(raw, list) else 0,
    )
    return SuggestResult(suggestions=suggestions, usage=_accumulate_usage(resp), note=note)
