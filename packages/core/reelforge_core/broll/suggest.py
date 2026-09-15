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

BROLL_PROMPT_VERSION = "b1"
MAX_CANDIDATES = 40
MAX_SUGGESTIONS = 8
MIN_LAYER_SEC = 1.5
MAX_LAYER_SEC = 6.0
MIN_SCENE_SEC = 1.0
# A scene that's already this much on screen as a main shot isn't B-roll.
MAIN_OVERLAP_FRAC = 0.3
# Mirrors the editor preview's buildSegments (reel-default xfade, hard cut).
DEFAULT_XFADE_SEC = 0.4
CUT_SEC = 0.04
LINE_GAP_SEC = 0.6
LINE_MAX_WORDS = 14
MAX_LINES = 200
THUMB_WIDTH = 320

SYSTEM_PROMPT = """You are a video editor adding B-roll to a talking-head reel.

You get the speaker's lines with their times on the reel timeline, the B-roll
already placed, and a catalog of candidates from the same project — video
scenes and photos, each with a thumbnail (video scenes also have an AI summary
and tags).

Propose cutaways that ILLUSTRATE what is being said: put a candidate over the
line it matches, starting as the relevant words begin.

Rules:
- Only use candidate ids from the catalog.
- Each placement lasts 1.5-6 seconds; placements never overlap each other or
  existing B-roll.
- Keep the speaker on screen for the first 2 seconds and for personal or
  emotional moments.
- Fewer, well-matched cutaways beat filling time: at most 8, and none at all
  if nothing genuinely matches.
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


def collect_candidates(
    timeline: ReelTimeline,
    videos: list[VideoSource],
    photos: list[PhotoSource],
    max_candidates: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """B-roll catalog: every photo, plus video scenes that aren't already on
    screen as main shots (a talking head's own scenes would just be more
    talking head). Balanced round-robin across sources under the cap. Pure
    apart from checking thumbnails exist."""
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
            length = sc.end_sec - sc.start_sec
            if length < MIN_SCENE_SEC:
                continue
            used = sum(
                max(0.0, min(sc.end_sec, b) - max(sc.start_sec, a))
                for a, b in main_ranges.get(v.asset_id, [])
            )
            if used / length > MAIN_OVERLAP_FRAC:
                continue
            s = sem.get(sc.index)
            thumb = v.working_dir / sc.thumbnail_path
            pool.append(
                Candidate(
                    id="",
                    kind="video",
                    asset_id=v.asset_id,
                    filename=v.filename,
                    start=round(sc.start_sec, 3),
                    end=round(sc.end_sec, 3),
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


def validate_suggestions(
    raw: list[Any],
    candidates: list[Candidate],
    program_sec: float,
    existing: list[tuple[float, float]],
) -> list[dict]:
    """Model output -> safe layer suggestions (PictureLayer fields + reason +
    quote), sorted by start. Anything unusable is dropped, never repaired
    into a different idea."""
    by_id = {c.id: c for c in candidates}
    taken = list(existing)
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
        dur = min(max(end - start, MIN_LAYER_SEC), MAX_LAYER_SEC)
        if c.kind == "video":
            if c.length < MIN_SCENE_SEC:
                continue
            dur = min(dur, c.length)
        if start + dur > program_sec:
            start = program_sec - dur
        if start < 0 or dur < MIN_SCENE_SEC:
            continue
        window = (round(start, 2), round(start + dur, 2))
        if any(_overlaps(window, t) for t in taken):
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
        out.append(
            {
                **layer.model_dump(exclude={"path"}),
                "filename": c.filename,
                "reason": str(item.get("reason", ""))[:300],
                "quote": str(item.get("quote", ""))[:200],
            }
        )
        if len(out) >= MAX_SUGGESTIONS:
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
    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
    resp = await _call_model(
        client,
        model=model,
        temperature=0.0,
        system_prompt=SYSTEM_PROMPT,
        messages=build_messages(lines, candidates, program_sec, existing, prompt),
        tools=[RECORD_BROLL],
        tool_name="record_broll",
        max_tokens=3000,
    )
    raw = _extract(resp)
    suggestions = validate_suggestions(raw, candidates, program_sec, existing)
    note = None if suggestions else "The AI didn't find B-roll that matches what's said."
    log.info(
        "broll: %d candidate(s), %d line(s) -> %d/%d suggestion(s) kept",
        len(candidates), len(lines), len(suggestions), len(raw) if isinstance(raw, list) else 0,
    )
    return SuggestResult(suggestions=suggestions, usage=_accumulate_usage(resp), note=note)
