"""AI emphasis: which words carry the point, and which were misheard.

One small stamped call per reel (`record_emphasis`, Haiku by default) sees
the reel's spoken lines and returns, per line:

- `emphasis` — the words a restrained caption highlights (replaces the
  keywords.py heuristic, which stays as the fallback and the floor top-up);
- `key_moment` — the single word the line lands on: that stretch of a
  talking head gets the tight framing, and a few get a pop (compose/sfx.py);
- `corrections` — misheard words ("skinboard" -> "skimboard"), one word for
  one word so every timing stays put.

Everything is validated locally (`apply_emphasis`); a failed or malformed
call means the heuristic captions of CP2 — emphasis can never fail a render.
Corrections are saved as the asset's transcript override, so they show (and
can be reverted) in the editor, and every later reel of that clip gets them.
Terms the model spells for us are kept per asset (`glossary.json`) and sent
back next time, so one clip is spelled the same way in every reel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from reelforge_core.models import AnalysisReport, RankedReel, UsageTotals

log = logging.getLogger(__name__)

EMPHASIS_VERSION = "e1"
LINE_MAX_WORDS = 16
LINE_GAP_SEC = 0.6
MAX_WORDS = 1800  # ~12 min of talk; later words keep heuristic captions
MAX_EMPHASIS_SHARE = 0.34  # per line, before the caption layer's 25% cap
MAX_CORRECTION_SHARE = 0.10
MIN_CORRECTION_SIMILARITY = 0.5
KEY_MOMENT_GAP_SEC = 6.0
KEY_MOMENT_GAP_LONG_SEC = 15.0
LONG_FORM_SEC = 180.0
TIGHT_ZOOM = 1.3
GLOSSARY_MAX = 100

SYSTEM_PROMPT = """You mark up the spoken words of a short social video \
for its captions and edit.

You get the video's title and its spoken lines (each line's words are \
numbered from 0) plus terms already known for this footage. For EVERY line \
return:

- emphasis: indices of the words a viewer should see highlighted — the ones \
that carry the point (a number, a result, a strong claim, a name, a \
contrast). Be restrained: across the video about 1 word in 6; many lines get \
none; never "the", "and", "you", filler, or a whole phrase.
- key_moment: the ONE word the line lands on, or null when the line is \
connective or filler. The edit tightens the shot there, so only mark lines \
that actually build to something.
- pop: true only where a pro editor would drop a subtle pop sound on the key \
moment — a number reveal, a punchline, a list step. Rare: a few per minute \
at most.
- corrections: words the speech recogniser clearly MISHEARD, as \
{index, text} with the right spelling of that ONE word (e.g. "skinboard" \
in a video about skimboarding is "skimboard"). Only real mishearings of a \
single word — never grammar, style, filler removal, rephrasing, or merging \
or splitting words. When unsure, leave it.

Also return glossary: domain terms, product and proper names spelled the way \
this video should spell them."""

RECORD_EMPHASIS: dict[str, Any] = {
    "name": "record_emphasis",
    "description": "Record caption emphasis, key moments and transcript fixes per line.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer"},
                        "emphasis": {"type": "array", "items": {"type": "integer"}},
                        "key_moment": {"type": ["integer", "null"]},
                        "pop": {"type": "boolean"},
                        "corrections": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "text": {"type": "string"},
                                },
                                "required": ["index", "text"],
                            },
                        },
                    },
                    "required": ["line", "emphasis"],
                },
            },
            "glossary": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["lines"],
    },
}


def _ms(t: float) -> int:
    return int(round(t * 1000))


@dataclass(frozen=True)
class SpokenWord:
    """One word as it plays in the reel."""

    asset_id: str
    start: float  # source seconds
    end: float
    text: str  # stripped of the transcriber's leading space
    mezz: float  # mezzanine start
    shot: int  # index into the rendered clips
    offset: float  # seconds from the shot's start

    @property
    def key(self) -> tuple[str, int]:
        return (self.asset_id, _ms(self.start))


@dataclass(frozen=True)
class Correction:
    asset_id: str
    start_ms: int
    old: str
    new: str


@dataclass
class Emphasis:
    emphasised: frozenset[tuple[str, int]] = frozenset()
    key_moments: list[SpokenWord] = field(default_factory=list)
    pops: list[float] = field(default_factory=list)  # mezzanine seconds
    corrections: list[Correction] = field(default_factory=list)
    glossary: list[str] = field(default_factory=list)
    source: str = "none"  # ai | cache | none

    def is_emphasised(self, asset_id: str, start: float) -> bool:
        return (asset_id, _ms(start)) in self.emphasised


# --------------------------------------------------------------------------
# Gathering the reel's words
# --------------------------------------------------------------------------


def reel_words(
    clips: list,
    analysis: AnalysisReport,
    analyses: dict[str, AnalysisReport | None] | None,
    xfades: list[float],
) -> list[SpokenWord]:
    """Every spoken word of the rendered shots, in mezzanine order, from the
    effective (override-applied) transcripts — the same mapping captions use."""
    from reelforge_core.compose.captions import clip_shots, effective_analyses, segment_words

    effective = effective_analyses(analysis, analyses)
    shots = clip_shots(clips, analysis.asset_id)
    n_cuts = max(0, len(shots) - 1)
    xf = list(xfades)[:n_cuts] + [0.0] * max(0, n_cuts - len(xfades))
    out: list[SpokenWord] = []
    cum = reclaimed = 0.0
    for i, (aid, s, e, dur) in enumerate(shots):
        if i > 0:
            reclaimed += xf[i - 1]
        mezz0 = cum - reclaimed
        rep = effective.get(aid) if aid is not None else None
        if rep is not None and s is not None and e is not None:
            for w in segment_words(rep, s, e):
                text = w.word.strip()
                if text:
                    out.append(
                        SpokenWord(aid, w.start, w.end, text, round(mezz0 + (w.start - s), 3), i,
                                   round(w.start - s, 3))
                    )
        cum += dur
    return out


def build_lines(words: list[SpokenWord]) -> list[list[SpokenWord]]:
    """Caption-sized lines: break at a sentence end, a pause, a shot change,
    or LINE_MAX_WORDS."""
    lines: list[list[SpokenWord]] = []
    cur: list[SpokenWord] = []
    for w in words[:MAX_WORDS]:
        if cur and (
            len(cur) >= LINE_MAX_WORDS
            or w.shot != cur[-1].shot
            or w.start - cur[-1].end > LINE_GAP_SEC
            or cur[-1].text.endswith((".", "!", "?"))
        ):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# Validation (pure)
# --------------------------------------------------------------------------

_EDGE_PUNCT = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def validate_correction(old: str, new: str, known: set[str]) -> str | None:
    """The corrected word with the original's punctuation, or None when the
    proposal isn't a plausible fix of ONE misheard word."""
    lead, core_old, trail = _EDGE_PUNCT.match(old).groups()
    core_new = _EDGE_PUNCT.match(new.strip()).group(2)
    if not core_new or any(ch.isspace() for ch in core_new) or core_new == core_old:
        return None
    similar = SequenceMatcher(None, core_old.lower(), core_new.lower()).ratio()
    if similar < MIN_CORRECTION_SIMILARITY and core_new.lower() not in known:
        return None
    return f"{lead}{core_new}{trail}"


def apply_emphasis(
    raw: dict,
    lines: list[list[SpokenWord]],
    known_terms: list[str],
    duration: float,
) -> Emphasis:
    """Keep only what validates. Bad entries are dropped one by one."""
    known = {t.lower() for t in known_terms}
    total_words = sum(len(ln) for ln in lines)
    max_fixes = max(3, int(total_words * MAX_CORRECTION_SHARE))
    emphasised: set[tuple[str, int]] = set()
    moments: list[tuple[SpokenWord, bool]] = []
    fixes: list[Correction] = []
    seen_lines: set[int] = set()
    for entry in raw.get("lines", []) or []:
        try:
            li = int(entry["line"])
            if li in seen_lines or not 0 <= li < len(lines):
                continue
            seen_lines.add(li)
            words = lines[li]
            cap = max(1, math.ceil(len(words) * MAX_EMPHASIS_SHARE))
            picked: list[int] = []
            for idx in entry.get("emphasis") or []:
                if isinstance(idx, int) and 0 <= idx < len(words) and idx not in picked:
                    picked.append(idx)
            km = entry.get("key_moment")
            if isinstance(km, int) and 0 <= km < len(words):
                # The key moment is always highlighted.
                picked = [km] + [i for i in picked if i != km]
                moments.append((words[km], bool(entry.get("pop"))))
            emphasised.update(words[i].key for i in picked[:cap])
            for c in entry.get("corrections") or []:
                if len(fixes) >= max_fixes:
                    break
                idx = c.get("index")
                if not isinstance(idx, int) or not 0 <= idx < len(words):
                    continue
                w = words[idx]
                new = validate_correction(w.text, str(c.get("text", "")), known)
                if new is not None:
                    fixes.append(Correction(w.asset_id, _ms(w.start), w.text, new))
        except (KeyError, TypeError, ValueError):
            continue

    gap = KEY_MOMENT_GAP_LONG_SEC if duration > LONG_FORM_SEC else KEY_MOMENT_GAP_SEC
    kept: list[SpokenWord] = []
    pops: list[float] = []
    for w, pop in sorted(moments, key=lambda m: m[0].mezz):
        if kept and w.mezz - kept[-1].mezz < gap:
            continue
        kept.append(w)
        if pop:
            pops.append(w.mezz)
    glossary = []
    for term in raw.get("glossary", []) or []:
        if isinstance(term, str) and 0 < len(term.strip()) <= 40:
            glossary.append(term.strip())
    return Emphasis(
        emphasised=frozenset(emphasised),
        key_moments=kept,
        pops=pops,
        corrections=fixes,
        glossary=glossary[:30],
        source="ai",
    )


# --------------------------------------------------------------------------
# What emphasis changes
# --------------------------------------------------------------------------


def tighten_framing(clips: list, emphasis: Emphasis) -> list:
    """Talking-head shots (the ones with framing keys) go tight for the
    phrase holding each key moment. The keys' TIMES never change, so neither
    does any duration; a moment already inside a tight stretch is left be."""
    by_shot: dict[int, list[SpokenWord]] = {}
    for w in emphasis.key_moments:
        by_shot.setdefault(w.shot, []).append(w)
    out = list(clips)
    for shot, moments in by_shot.items():
        if not 0 <= shot < len(out) or not out[shot].framing_keys:
            continue
        keys = [tuple(k) for k in out[shot].framing_keys]
        for w in moments:
            k = max((i for i, key in enumerate(keys) if key[0] <= w.offset + 1e-6), default=0)
            t, zoom, cx, cy = keys[k]
            if zoom >= TIGHT_ZOOM or (k > 0 and keys[k - 1][1] >= TIGHT_ZOOM):
                continue
            keys[k] = (t, TIGHT_ZOOM, cx, cy)
            if k + 1 < len(keys) and keys[k + 1][1] >= TIGHT_ZOOM:
                # Two tight keys in a row would be no visible change.
                nt, _, ncx, ncy = keys[k + 1]
                keys[k + 1] = (nt, 1.0, ncx, ncy)
        out[shot] = replace(out[shot], framing_keys=tuple(keys))
    return out


def save_corrections(
    corrections: list[Correction],
    effective: dict[str, AnalysisReport | None],
) -> int:
    """Write the fixes into each asset's transcript override (created from
    the analysis transcript when there is none). Only words still reading
    the old text change, so replaying a cached result is a no-op."""
    from reelforge_core import transcript_store

    by_asset: dict[str, dict[int, Correction]] = {}
    for c in corrections:
        by_asset.setdefault(c.asset_id, {})[c.start_ms] = c
    changed_total = 0
    for aid, fixes in by_asset.items():
        rep = effective.get(aid)
        if rep is None or rep.transcript is None:
            continue
        transcript = rep.transcript.model_copy(deep=True)
        changed = 0
        for seg in transcript.segments:
            seg_changed = False
            for w in seg.words:
                fix = fixes.get(_ms(w.start))
                if fix is None or w.word.strip() != fix.old:
                    continue
                lead = w.word[: len(w.word) - len(w.word.lstrip())]
                w.word = lead + fix.new
                seg_changed = True
                changed += 1
            if seg_changed:
                seg.text = "".join(w.word for w in seg.words).strip()
        if changed:
            transcript_store.validate_transcript(transcript)
            transcript_store._save_sync(aid, transcript)
            changed_total += changed
            log.info("emphasis: %d transcript fix(es) saved for %s", changed, aid[:12])
    return changed_total


# --------------------------------------------------------------------------
# Glossary + stamp
# --------------------------------------------------------------------------


def _glossary_path(working_root: Path, asset_id: str) -> Path:
    return working_root / asset_id / "glossary.json"


def load_glossary(working_root: Path, asset_ids: list[str]) -> list[str]:
    terms: list[str] = []
    for aid in asset_ids:
        try:
            data = json.loads(_glossary_path(working_root, aid).read_text())
            terms.extend(t for t in data.get("terms", []) if isinstance(t, str))
        except (OSError, ValueError):
            continue
    return sorted(set(terms), key=str.lower)[:GLOSSARY_MAX]


def save_glossary(working_root: Path, asset_ids: list[str], terms: list[str]) -> None:
    from reelforge_core.io_utils import write_json_atomic

    if not terms:
        return
    for aid in asset_ids:
        path = _glossary_path(working_root, aid)
        if not path.parent.is_dir():
            continue
        merged = sorted(set(load_glossary(working_root, [aid])) | set(terms), key=str.lower)
        write_json_atomic(path, {"terms": merged[:GLOSSARY_MAX]})


def fingerprint(lines: list[list[SpokenWord]], model: str, title: str) -> str:
    payload = json.dumps(
        [EMPHASIS_VERSION, model, title, [[[w.asset_id, _ms(w.start), w.text] for w in ln] for ln in lines]],
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode()).hexdigest()


def _corrected_lines(lines: list[list[SpokenWord]], fixes: list[Correction]) -> list[list[SpokenWord]]:
    new_text = {(c.asset_id, c.start_ms): c.new for c in fixes}
    return [[replace(w, text=new_text.get(w.key, w.text)) for w in ln] for ln in lines]


def build_context(reel: RankedReel, lines: list[list[SpokenWord]], known_terms: list[str]) -> dict:
    return {
        "title": reel.title,
        "hook": reel.hook,
        "known_terms": known_terms,
        "lines": [
            {"line": i, "t": round(ln[0].mezz, 1), "words": [w.text for w in ln]}
            for i, ln in enumerate(lines)
        ],
    }


def _extract(resp: Any) -> dict:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            inp = getattr(block, "input", None)
            if isinstance(inp, str):
                inp = json.loads(inp)
            if isinstance(inp, dict) and "lines" in inp:
                return inp
    raise ValueError("no record_emphasis tool_use block in response")


def _default_client() -> Any:
    """The live API client. Tests replace this (tests/conftest.py) so no
    compose test ever makes a real call — the test service loads .env."""
    from anthropic import AsyncAnthropic

    return AsyncAnthropic()


async def run_emphasis(
    words: list[SpokenWord],
    *,
    reel: RankedReel,
    model: str,
    reel_dir: Path,
    working_root: Path,
    duration: float,
    client: Any | None = None,
) -> tuple[Emphasis, UsageTotals]:
    """Stamped, best-effort. Returns an empty Emphasis (source "none") on any
    failure — captions then use the keywords.py heuristic."""
    from reelforge_core.io_utils import write_json_atomic
    from reelforge_core.reels.rank import _accumulate_usage, _call_model

    lines = build_lines(words)
    if not lines:
        return Emphasis(), UsageTotals()
    asset_ids = sorted({w.asset_id for w in words})
    known = load_glossary(working_root, asset_ids)
    raw_path = reel_dir / "emphasis_raw.json"
    fp = fingerprint(lines, model, reel.title)

    # A replay matches either the words the call saw or the words after its
    # own corrections were saved — otherwise every fix would buy a new call.
    try:
        cached = json.loads(raw_path.read_text())
        if fp in cached.get("fingerprints", []):
            result = apply_emphasis(cached.get("raw", {}), lines, known + cached.get("glossary", []), duration)
            result.source = "cache"
            log.info("emphasis: stamp hit (%d key word(s))", len(result.emphasised))
            return result, UsageTotals()
    except (OSError, ValueError):
        pass

    try:
        if client is None:
            client = _default_client()
        resp = await _call_model(
            client,
            model=model,
            temperature=0.0,
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(build_context(reel, lines, known))}],
            tools=[RECORD_EMPHASIS],
            tool_name="record_emphasis",
            max_tokens=8000,
        )
        raw = _extract(resp)
        usage = _accumulate_usage(resp)
    except Exception as exc:  # noqa: BLE001
        log.warning("emphasis call failed; heuristic captions: %s", exc)
        return Emphasis(), UsageTotals()

    result = apply_emphasis(raw, lines, known, duration)
    fps = [fp]
    if result.corrections:
        fps.append(fingerprint(_corrected_lines(lines, result.corrections), model, reel.title))
    write_json_atomic(
        raw_path,
        {"version": EMPHASIS_VERSION, "model": model, "fingerprints": fps, "raw": raw,
         "glossary": result.glossary, "usage": usage.model_dump()},
    )
    try:
        save_glossary(working_root, asset_ids, result.glossary)
    except OSError as exc:  # pragma: no cover
        log.warning("glossary not saved: %s", exc)
    log.info(
        "emphasis: %d key word(s), %d key moment(s), %d pop(s), %d fix(es) (in %d / out %d tokens)",
        len(result.emphasised), len(result.key_moments), len(result.pops), len(result.corrections),
        usage.input_tokens, usage.output_tokens,
    )
    return result, usage
