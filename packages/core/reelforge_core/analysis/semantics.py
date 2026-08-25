"""Per-scene semantic metadata from Claude via forced tool-use, with SQLite caching."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from reelforge_core import db
from reelforge_core.errors import SemanticsError
from reelforge_core.ingest import MediaAsset
from reelforge_core.io_utils import write_json_atomic
from reelforge_core.models import (
    AnalysisConfig,
    MOOD_VALUES,
    ProgressCallback,
    ProgressEvent,
    Scene,
    SceneSemantics,
    Transcript,
    UsageTotals,
    compute_overall,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT_V1 = (
    "You analyze single scenes from a longer video to produce structured metadata "
    "for a short-form video editor. You see ONE representative frame and optionally "
    "a transcript slice covering the scene's timespan. Respond ONLY by calling the "
    "record_scene_analysis tool. Do not include any other text. Keep the summary "
    "under 15 words. Tags are lowercase, 3-7 items, no hashtags. Choose mood strictly "
    "from the provided enum."
)

RECORD_SCENE_ANALYSIS: dict[str, Any] = {
    "name": "record_scene_analysis",
    "description": "Record the analysis of a single video scene.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 120},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 7,
            },
            "mood": {"type": "string", "enum": list(MOOD_VALUES)},
            "has_speech": {"type": "boolean"},
            "visual_energy": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["summary", "tags", "mood", "has_speech", "visual_energy"],
    },
}

MAX_RETRIES = 5


def _transcript_slice(transcript: Transcript | None, start: float, end: float) -> str:
    if transcript is None:
        return ""
    parts: list[str] = []
    for seg in transcript.segments:
        if seg.end < start or seg.start > end:
            continue
        parts.append(seg.text.strip())
    return " ".join(parts).strip()


def _read_thumb(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    return data, db.sha256_bytes(data)


def _make_user_message(
    scene_index: int, total: int, scene: Scene, thumb_b64: str, transcript_text: str
) -> list[dict[str, Any]]:
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": thumb_b64},
        },
        {
            "type": "text",
            "text": (
                f"Scene {scene_index} of {total}. "
                f"Timespan: {scene.start_sec:.1f}s-{scene.end_sec:.1f}s. "
                f"Transcript for this timespan:\n\n{transcript_text or '(no speech)'}"
            ),
        },
    ]


def _is_retryable(exc: Exception) -> bool:
    # Lazy import so tests can inject fake exception types.
    try:
        import anthropic  # type: ignore
    except Exception:  # pragma: no cover
        return False
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500
    return False


async def _call_claude_with_retries(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_message: list[dict[str, Any]],
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[RECORD_SCENE_ANALYSIS],
                tool_choice={"type": "tool", "name": "record_scene_analysis"},
            )
            if getattr(resp, "stop_reason", None) != "tool_use":
                # One corrective retry: sometimes the model claims stop without a
                # tool_use block even though tool_choice was forced.
                if attempt + 1 < MAX_RETRIES:
                    log.warning(
                        "unexpected stop_reason=%s; retrying",
                        getattr(resp, "stop_reason", None),
                    )
                    last_exc = SemanticsError(
                        f"stop_reason={getattr(resp, 'stop_reason', None)}"
                    )
                    await asyncio.sleep(min(60, 2**attempt + random.random()))
                    continue
            return resp
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise SemanticsError(f"non-retryable Anthropic error: {exc}") from exc
            delay = min(60, 2**attempt + random.random())
            log.warning(
                "semantics call retry %d/%d in %.2fs due to %s",
                attempt + 1,
                MAX_RETRIES,
                delay,
                exc.__class__.__name__,
            )
            await asyncio.sleep(delay)
    raise SemanticsError(
        f"semantics call failed after {MAX_RETRIES} retries: {last_exc}"
    ) from last_exc


def _extract_tool_input(resp: Any) -> dict:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            data = block.input
            if isinstance(data, str):
                return json.loads(data)
            return dict(data)
    raise SemanticsError("no tool_use block in response")


async def analyze_semantics(
    asset: MediaAsset,
    scenes: list[Scene],
    transcript: Transcript | None,
    working_dir: Path,
    config: AnalysisConfig,
    progress: ProgressCallback,
    *,
    client: Any | None = None,
) -> tuple[list[SceneSemantics], UsageTotals]:
    if not scenes:
        write_json_atomic(working_dir / "semantics.json", [])
        await progress(ProgressEvent("semantics", 1.0, compute_overall("semantics", 1.0)))
        return [], UsageTotals()

    # Precompute cache keys per scene.
    cache_inputs: list[tuple[Scene, str, bytes, str]] = []  # (scene, key, thumb_bytes, slice)
    keys: list[str] = []
    for scene in scenes:
        thumb_path = working_dir / scene.thumbnail_path
        thumb_bytes, thumb_hash = _read_thumb(thumb_path)
        slice_text = _transcript_slice(transcript, scene.start_sec, scene.end_sec)
        slice_hash = db.sha256_text(slice_text)
        key = db.semantics_cache_key(
            asset_id=asset.id,
            scene_index=scene.index,
            model=config.semantics_model,
            prompt_version=config.semantics_prompt_version,
            thumb_sha256=thumb_hash,
            transcript_slice_sha256=slice_hash,
        )
        cache_inputs.append((scene, key, thumb_bytes, slice_text))
        keys.append(key)

    hits_map = await db.fetch_semantics(keys)
    log.info("semantics cache: %d hits / %d scenes", len(hits_map), len(scenes))

    # Lazy client construction.
    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()

    results: list[SceneSemantics | None] = [None] * len(scenes)
    completed = 0
    total = len(scenes)
    sem = asyncio.Semaphore(max(1, config.semantics_concurrency))

    usage = UsageTotals()
    usage_lock = asyncio.Lock()
    last_emit = 0.0

    async def emit_progress() -> None:
        nonlocal last_emit
        now = time.monotonic()
        frac = completed / total if total else 1.0
        if frac < 1.0 and now - last_emit < 0.25:
            return
        last_emit = now
        await progress(
            ProgressEvent("semantics", frac, compute_overall("semantics", frac))
        )

    async def run_miss(scene: Scene, key: str, thumb_bytes: bytes, slice_text: str) -> None:
        nonlocal completed
        async with sem:
            b64 = base64.b64encode(thumb_bytes).decode("ascii")
            msg = _make_user_message(scene.index, total, scene, b64, slice_text)
            resp = await _call_claude_with_retries(
                client,
                model=config.semantics_model,
                system_prompt=SYSTEM_PROMPT_V1,
                user_message=msg,
            )
            data = _extract_tool_input(resp)
            sem_obj = SceneSemantics(scene_index=scene.index, cached=False, **data)
            results[scene.index] = sem_obj
            await db.upsert_semantics(
                cache_key=key,
                result=sem_obj.model_dump(exclude={"cached"}),
                model=config.semantics_model,
                prompt_version=config.semantics_prompt_version,
            )
            resp_usage = getattr(resp, "usage", None)
            if resp_usage is not None:
                async with usage_lock:
                    usage.input_tokens += int(getattr(resp_usage, "input_tokens", 0) or 0)
                    usage.output_tokens += int(getattr(resp_usage, "output_tokens", 0) or 0)
        completed += 1
        await emit_progress()

    tasks: list[asyncio.Task] = []
    for scene, key, thumb_bytes, slice_text in cache_inputs:
        if key in hits_map:
            cached = SceneSemantics(**hits_map[key], cached=True)
            # Guard against cache rows that predate the scene_index schema
            cached = cached.model_copy(update={"scene_index": scene.index})
            results[scene.index] = cached
            completed += 1
            usage.cache_hits += 1
            await emit_progress()
        else:
            tasks.append(asyncio.create_task(run_miss(scene, key, thumb_bytes, slice_text)))

    if tasks:
        await asyncio.gather(*tasks)

    final: list[SceneSemantics] = []
    for i, r in enumerate(results):
        if r is None:
            raise SemanticsError(f"scene {i} produced no semantics result")
        final.append(r)

    write_json_atomic(
        working_dir / "semantics.json", [s.model_dump() for s in final]
    )
    await progress(ProgressEvent("semantics", 1.0, compute_overall("semantics", 1.0)))
    return final, usage
