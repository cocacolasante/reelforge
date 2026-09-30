"""Automatic B-roll for scene-mode reels (pro-editing CP9).

The editor's AI assistant (broll/suggest.py) proposed cutaways only when
asked. Here compose asks on its own, once the shot list is FINAL (after the
style plan, director, beat trims and extraction), so the cutaways are timed
to what actually renders:

1. `timeline_from_clips` turns the rendered clips into the ReelTimeline
   `suggest_broll` already understands — its placement math is the editor
   preview's, so a layer lands on the same words in both.
2. One stamped `record_broll` call (the director model) with an automatic
   density: about one cutaway per SHORT_SEC_PER seconds on shorts, one per
   LONG_SEC_PER on long-form, then `validate_suggestions` as always.
3. The layers render through the existing final-pass compositor
   (`build_final_command(layers=...)`) and are recorded in compose.json
   (`auto_broll`), where the editor's default timeline picks them up — so
   they are visible and removable there.

Only for talky, non-hype reels in the smart flow, and only when the project
has other clips or photos (`ComposeConfig.broll_sources`). Any failure means
no B-roll, never a failed render.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any

from reelforge_core.models import (
    AnalysisReport,
    BrollSource,
    ComposeConfig,
    PictureLayer,
    ReelTimeline,
    TimelineShot,
    TransitionStyle,
)

log = logging.getLogger(__name__)

AUTO_BROLL_VERSION = "a1"
SHORT_SEC_PER = 8.0
LONG_SEC_PER = 20.0
LONG_FORM_SEC = 180.0
MIN_SPEECH_RATIO = 0.4


def auto_budget(program_sec: float) -> int:
    """Cutaways for a reel of this length: ~1 per 8s on shorts, ~1 per 20s
    on long-form, capped so it scales with the reel. Pure."""
    per = LONG_SEC_PER if program_sec > LONG_FORM_SEC else SHORT_SEC_PER
    cap = max(20, int(program_sec // LONG_SEC_PER))
    return max(1, min(cap, math.floor(program_sec / per)))


def wants_auto_broll(config: ComposeConfig, style: str, speech_ratio: float) -> bool:
    if not config.broll_sources or config.auto_broll == "off":
        return False
    if config.auto_broll == "on":
        return True
    return config.smart_mode and style != "hype" and speech_ratio >= MIN_SPEECH_RATIO


def timeline_from_clips(
    clips: list, transitions: list[tuple[str, float]], default_asset_id: str
) -> ReelTimeline:
    """The rendered shot list as a ReelTimeline (paths blank). Pure."""
    shots: list[TimelineShot] = []
    for i, c in enumerate(clips):
        tr = None
        if i < len(transitions) and i < len(clips) - 1:
            kind, dur = transitions[i]
            tr = TransitionStyle(kind=kind, duration_sec=max(0.04, dur))
        if c.is_photo:
            shots.append(TimelineShot(kind="photo", asset_id=c.photo_asset_id or "photo",
                                      duration_sec=c.duration, transition_after=tr))
        else:
            shots.append(TimelineShot(
                kind="video", asset_id=c.asset_id or default_asset_id,
                in_ts=c.in_ts, out_ts=c.out_ts, speed=c.speed, transition_after=tr,
            ))
    return ReelTimeline(shots=shots)


def _fingerprint(timeline: ReelTimeline, sources: list[BrollSource], model: str, budget: int) -> str:
    blob = json.dumps(
        [
            AUTO_BROLL_VERSION, model, budget,
            [[s.asset_id, round(s.in_ts, 3), round(s.out_ts, 3), s.kind] for s in timeline.shots],
            sorted(src.asset_id for src in sources),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha1(blob.encode()).hexdigest()


def to_layers(suggestions: list[dict], sources: list[BrollSource]) -> list[PictureLayer]:
    """Validated suggestion dicts -> PictureLayers with resolved paths.
    Suggestions for sources we can't resolve are dropped. Pure."""
    by_id = {s.asset_id: s for s in sources}
    out: list[PictureLayer] = []
    for i, sug in enumerate(suggestions):
        src = by_id.get(sug.get("asset_id", ""))
        if src is None:
            continue
        try:
            fields = {k: v for k, v in sug.items() if k in PictureLayer.model_fields}
            fields.update(id=f"auto-{i + 1}", path=src.path, kind=src.kind)
            out.append(PictureLayer(**fields))
        except Exception:  # noqa: BLE001
            continue
    return out


def _default_client() -> Any:
    """Replaced in tests (tests/conftest.py) — the test service loads .env."""
    from anthropic import AsyncAnthropic

    return AsyncAnthropic()


async def plan_auto_broll(
    clips: list,
    transitions: list[tuple[str, float]],
    analysis: AnalysisReport,
    config: ComposeConfig,
    *,
    reel_dir: Path,
    working_root: Path,
    client: Any | None = None,
) -> list[PictureLayer]:
    """The cutaways for this render, stamped (`auto_broll.json`). [] on any
    failure or when nothing matches."""
    from reelforge_core.broll.suggest import (
        PhotoSource,
        VideoSource,
        photo_thumbnail,
        program_duration,
        suggest_broll,
    )
    from reelforge_core.io_utils import write_json_atomic

    timeline = timeline_from_clips(clips, transitions, analysis.asset_id)
    program_sec = program_duration(timeline)
    budget = auto_budget(program_sec)
    model = config.director_model
    fp = _fingerprint(timeline, config.broll_sources, model, budget)
    cache = reel_dir / "auto_broll.json"
    try:
        data = json.loads(cache.read_text())
        if data.get("fingerprint") == fp:
            return to_layers(data.get("suggestions", []), config.broll_sources)
    except (OSError, ValueError):
        pass

    try:
        from reelforge_core.transcript_store import load_override_sync

        videos: list[VideoSource] = []
        transcripts: dict = {}
        photos: list[PhotoSource] = []
        wanted = {analysis.asset_id} | {s.asset_id for s in timeline.shots if s.kind == "video"}
        for src in config.broll_sources:
            wd = working_root / src.asset_id
            if src.kind == "photo":
                photos.append(PhotoSource(src.asset_id, src.filename,
                                          photo_thumbnail(Path(src.path), wd)))
                continue
            try:
                rep = AnalysisReport.model_validate_json((wd / "analysis.json").read_text())
            except (OSError, ValueError):
                continue  # not analyzed: nothing to describe it by
            videos.append(VideoSource(src.asset_id, src.filename, rep, wd))
            wanted.add(src.asset_id)
        for aid in wanted:
            rep = analysis if aid == analysis.asset_id else next(
                (v.analysis for v in videos if v.asset_id == aid), None)
            try:
                override = load_override_sync(aid)
            except Exception:  # noqa: BLE001
                override = None
            transcripts[aid] = override or (rep.transcript if rep is not None else None)
        if client is None:
            client = _default_client()
        res = await suggest_broll(timeline, videos, photos, transcripts, model=model,
                                  client=client, budget=budget)
    except Exception as exc:  # noqa: BLE001 — B-roll can never fail a render
        log.warning("auto B-roll skipped: %s", exc)
        return []
    write_json_atomic(cache, {"version": AUTO_BROLL_VERSION, "fingerprint": fp,
                              "suggestions": res.suggestions, "note": res.note,
                              "usage": res.usage.model_dump()})
    log.info("auto B-roll: %d cutaway(s) (budget %d; %s)", len(res.suggestions), budget,
             res.note or "ok")
    return to_layers(res.suggestions, config.broll_sources)
