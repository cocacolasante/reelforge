"""Score one rendered reel against the targets and write `qa.json`.

Reads only what compose leaves in the reel directory — `compose.json`,
`words.json`, `captions.ass`, `mezzanine.mp4` — so it can score any reel,
fresh or old. Missing inputs make their metrics `n/a`, never an error:
the scorecard runs at the end of every render and must not fail one.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from reelforge_core.compose.safezone import safe_rect
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.qa import metrics as m
from reelforge_core.qa.captions_geom import parse_ass
from reelforge_core.qa.thresholds import BY_KIND

log = logging.getLogger(__name__)

QA_VERSION = "q3"  # q2: sfx_per_min; q3: face_in_crop
_LUFS = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS")


def measure_loudness(path: Path) -> tuple[float | None, float | None]:
    """(integrated LUFS, true peak dBTP) from ffmpeg's EBU R128 summary."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    summary = proc.stderr.rsplit("Summary:", 1)[-1]
    lufs = _LUFS.search(summary)
    peak = _PEAK.search(summary)
    return (
        float(lufs.group(1)) if lufs else None,
        (float("-inf") if peak.group(1) == "-inf" else float(peak.group(1))) if peak else None,
    )


def _shots(manifest: dict) -> tuple[list[m.Shot], bool]:
    """Shots from compose.json. Returns (shots, precise): manifests written
    before per-shot metadata existed lack asset/zoom/transition, so every
    same-scene junction looks contiguous — flagged as an estimate."""
    entries = manifest.get("scene_clip_map") or []
    precise = bool(entries) and all("duration" in e for e in entries)
    reel_asset = manifest.get("asset_id")
    shots: list[m.Shot] = []
    for e in entries:
        photo = e.get("kind") == "photo"
        dur = e.get("duration")
        if dur is None and e.get("out_ts") is not None and e.get("in_ts") is not None:
            dur = float(e["out_ts"]) - float(e["in_ts"])
        transition = e.get("transition_after") or [None, 0.0]
        shots.append(
            m.Shot(
                asset_id=e.get("photo_asset_id") if photo else (e.get("asset_id") or reel_asset),
                in_ts=None if photo else e.get("in_ts"),
                out_ts=None if photo else e.get("out_ts"),
                duration=float(dur or 0.0),
                zoom=float(e.get("punch_in") or 1.0),
                is_photo=photo,
                transition_sec=float(transition[1] or 0.0),
                transition_kind=transition[0],
                framing=tuple((float(k[0]), float(k[1])) for k in (e.get("framing_keys") or [])),
            )
        )
    return shots, precise


def _words(path: Path) -> list[m.Word] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    words = [
        m.Word(float(w["start"]), float(w["end"]), str(w["word"]), w.get("shot"))
        for w in raw.get("words", [])
    ]
    return sorted(words, key=lambda w: w.start)


def build_scorecard(reel_dir: Path, *, loudness: bool = True) -> dict:
    manifest = json.loads((reel_dir / "compose.json").read_text())
    duration = float(manifest.get("duration_sec") or 0.0)
    width = int(manifest.get("width") or 1080)
    height = int(manifest.get("height") or 1920)
    style = manifest.get("style") or (manifest.get("config") or {}).get("style")

    shots, precise = _shots(manifest)
    layers = [(float(a), float(b)) for a, b in (manifest.get("layers") or [])]
    changes, invisible = m.visible_changes(shots, layers, duration)

    words = _words(reel_dir / "words.json")
    ratio = m.speech_ratio(words, duration) if words is not None else None
    kind = m.content_kind(duration, ratio or 0.0, style)

    measured: dict = {
        "changes_per_min": m.changes_per_minute(changes, duration),
        "longest_static_sec": m.longest_static(changes, duration),
        "invisible_junctions": invisible,
        "flashy_transition_share": m.flashy_share(shots) if precise else None,
        "extra_flashy_transitions": m.extra_flashy(shots) if precise else None,
    }
    # Face in crop (CP5): duration-weighted over shots the tracker saw a face in.
    faced = [
        (float(e.get("duration") or 0.0), float(e["face_coverage"]))
        for e in manifest.get("scene_clip_map") or []
        if e.get("face_coverage") is not None
    ]
    if faced and sum(d for d, _ in faced) > 0:
        measured["face_in_crop"] = round(
            sum(d * c for d, c in faced) / sum(d for d, _ in faced), 3
        )
    if "sfx" in manifest:  # renders from before CP4 didn't record effects
        measured["sfx_per_min"] = round(len(manifest["sfx"]) / max(duration / 60.0, 1e-6), 2)

    if words:
        air = m.dead_air(words)
        end = m.ending(words, duration)
        measured.update(
            {
                "hook_latency_sec": m.hook_latency(words),
                "greeting": 1 if m.greeting_in_opening(words) else 0,
                "dead_air_longest_sec": air["longest"],
                "dead_air_percent": air["percent"],
                "fillers_per_min": m.filler_rate(words, duration),
                "trailing_filler": 1 if end.get("trailing_filler") else 0,
            }
        )
        details_speech = {"dead_air": air, "ending": end, "opening_greeting": m.greeting_in_opening(words)}
    else:
        details_speech = {}

    captions = reel_dir / "captions.ass"
    caption_details: dict = {}
    if captions.exists():
        try:
            doc = parse_ass(captions)
            caption_details = m.caption_stats(doc.boxes, safe_rect(doc.width, doc.height))
            if caption_details["captions"]:
                measured["words_per_caption_p95"] = caption_details["words_per_caption_p95"]
                measured["highlighted_share"] = caption_details["highlighted_share"]
            measured["safe_zone_violations"] = caption_details["safe_zone_violations"]
        except Exception as exc:  # noqa: BLE001 — a malformed file is n/a, not a crash
            log.warning("qa: could not read %s: %s", captions, exc)

    mezz = reel_dir / "mezzanine.mp4"
    if loudness and mezz.exists():
        lufs, peak = measure_loudness(mezz)
        measured["integrated_lufs"] = lufs
        measured["true_peak_dbtp"] = peak

    checks = []
    for target in BY_KIND[kind]:
        value = measured.get(target.metric)
        checks.append(
            {
                "metric": target.metric,
                "value": value,
                "target": f"{target.op} {target.value}",
                "result": target.check(value),
                "note": target.note,
            }
        )
    passed = sum(1 for c in checks if c["result"] == "pass")
    applicable = sum(1 for c in checks if c["result"] != "n/a")

    return {
        "qa_version": QA_VERSION,
        "reel_id": manifest.get("reel_id"),
        "title": manifest.get("reel_title"),
        "kind": kind,
        "style": style,
        "duration_sec": round(duration, 2),
        "speech_ratio": ratio,
        "precise": precise,
        "score": f"{passed}/{applicable}",
        "checks": checks,
        "changes": [{"t": c.t, "why": c.why} for c in changes],
        "speech": details_speech,
        "captions": caption_details,
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }


def write_scorecard(reel_dir: Path, *, loudness: bool = True) -> dict:
    card = build_scorecard(reel_dir, loudness=loudness)
    write_json_atomic(reel_dir / "qa.json", card)
    return card


def format_scorecard(card: dict) -> str:
    """A terminal table: one line per check, failures marked."""
    mark = {"pass": "ok  ", "fail": "FAIL", "n/a": " -- "}
    lines = [
        f"{card.get('title') or card.get('reel_id')}  "
        f"[{card['kind']}, {card['duration_sec']}s, score {card['score']}]"
        + ("" if card.get("precise") else "  (estimated: rendered before per-shot metadata)")
    ]
    for c in card["checks"]:
        value = c["value"]
        shown = "--" if value is None else (f"{value:.2f}" if isinstance(value, float) else str(value))
        lines.append(f"  {mark[c['result']]}  {c['metric']:<24} {shown:>8}   target {c['target']}")
    return "\n".join(lines)
