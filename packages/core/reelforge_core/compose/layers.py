"""Picture layers (B-roll): clips and photos drawn OVER the main track.

A layer covers [start, end] of the mezzanine — full frame or a
picture-in-picture box — while the main track keeps its picture timing and its
audio plays on underneath. Layers never change shot durations, so captions,
beat sync and the xfade math are untouched. Each layer is pre-rendered here to
a short clip at its box size (cached like shots), then composited once in the
final render pass (graph_builder: after the crossfade chain, before grade and
captions) — never inside hierarchical chunk parts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from reelforge_core import cache as file_cache
from reelforge_core.compose.graph import run_ffmpeg
from reelforge_core.ingest import MediaAsset
from reelforge_core.models import ComposeConfig, PhotoInsert, PictureLayer

log = logging.getLogger(__name__)

# Gap between a picture-in-picture box and the frame edge, as a fraction of
# the frame's shorter side.
PIP_MARGIN_FRAC = 0.04
MIN_LAYER_SEC = 0.1


@dataclass(frozen=True)
class LayerInput:
    """A rendered layer, ready for the final graph: its clip starts at
    mezzanine `start` and is composited at (x, y) until `end`."""

    path: Path
    start: float
    end: float
    x: int
    y: int
    fade: float


def layer_box(layer: PictureLayer, width: int, height: int) -> tuple[int, int, int, int]:
    """(box width, box height, x, y) for a layer on a width x height frame.
    Boxes keep the frame's aspect and even dimensions. Pure."""
    if layer.mode == "full":
        return width, height, 0, 0
    bw = max(2, int(width * layer.pip_scale / 2) * 2)
    bh = max(2, int(height * layer.pip_scale / 2) * 2)
    margin = int(round(min(width, height) * PIP_MARGIN_FRAC))
    x = margin if layer.pip_corner in ("tl", "bl") else width - bw - margin
    y = margin if layer.pip_corner in ("tl", "tr") else height - bh - margin
    return bw, bh, x, y


def layer_window(
    layer: PictureLayer, program_sec: float, source_sec: float | None = None
) -> tuple[float, float] | None:
    """The mezzanine window a layer actually covers: clamped to the program,
    and for video to what's left of the source after `in_ts`. None when too
    short to show. Pure."""
    start = max(0.0, layer.start_sec)
    end = min(layer.end_sec, program_sec)
    if layer.kind == "video" and source_sec is not None:
        end = min(end, start + max(0.0, source_sec - max(0.0, layer.in_ts)))
    if end - start < MIN_LAYER_SEC:
        return None
    return round(start, 3), round(end, 3)


def layer_fade(layer: PictureLayer, window_sec: float) -> float:
    """Fade in/out length: the layer's fade, never more than a third of it."""
    return round(min(layer.fade_ms / 1000.0, window_sec / 3.0), 3)


def _link_cached(cache_key: str, out_path: Path) -> bool:
    cached = file_cache.lookup(cache_key)
    if cached is None:
        return False
    try:
        if out_path.exists():
            out_path.unlink()
        os.link(cached, out_path)
    except OSError:
        shutil.copy2(cached, out_path)
    return True


def _store_cached(cache_key: str, out_path: Path) -> None:
    try:
        target = file_cache.path_for("clip", cache_key, "mp4")
        shutil.copy2(out_path, target)
        file_cache.register(cache_key, "clip", target)
        file_cache.evict_if_over_cap("clip", file_cache.cap_from_env("clip", 20.0))
    except Exception:  # pragma: no cover
        log.exception("layer cache write failed for %s", cache_key)


async def extract_layer_clips(
    layers: list[PictureLayer],
    sources: dict[str, MediaAsset],
    config: ComposeConfig,
    reel_dir: Path,
    log_file: Path,
    program_sec: float,
) -> list[LayerInput]:
    """Render each layer to `clips/layer_NNNN.mp4` at its box size. Layers
    that fall outside the program (or past their source's end) are skipped."""
    from reelforge_core.compose.clips import HDR_TRANSFERS, build_clip_command
    from reelforge_core.compose.photos import render_photo_clip
    from reelforge_core.compose.reframe import estimate_pan, should_crop

    clips_dir = reel_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    frame_w, frame_h = config.resolution
    sem = asyncio.Semaphore(4)

    async def _one(k: int, layer: PictureLayer) -> LayerInput | None:
        bw, bh, x, y = layer_box(layer, frame_w, frame_h)
        box_config = config.model_copy(update={"target_resolution": (bw, bh)})
        out_path = clips_dir / f"layer_{k:04d}.mp4"
        if layer.kind == "photo":
            window = layer_window(layer, program_sec)
            if window is None:
                return None
            insert = PhotoInsert(
                asset_id=layer.asset_id,
                path=layer.path,
                position=k,
                duration_sec=window[1] - window[0],
                ken_burns=layer.ken_burns,
            )
            async with sem:
                await render_photo_clip(insert, out_path, box_config, log_file, pan_index=k)
        else:
            asset = sources[layer.asset_id]
            window = layer_window(layer, program_sec, asset.probe.duration_s or None)
            if window is None:
                return None
            in_ts = max(0.0, layer.in_ts)
            out_ts = in_ts + (window[1] - window[0])
            is_hdr = (asset.probe.color_transfer or "") in HDR_TRANSFERS
            pan = None
            # Fill the box (subject-tracked crop) rather than letterboxing
            # B-roll inside it, whenever the source is wider than the box.
            if should_crop(asset.probe.width or 0, asset.probe.height or 0, bw, bh, "auto"):
                pan = await asyncio.to_thread(estimate_pan, asset.path, in_ts, out_ts)
            cache_key = file_cache.compute_key(
                "clip",
                {
                    "asset_id": asset.id,
                    "scene_idx": -1,
                    "source_mtime": int(asset.path.stat().st_mtime),
                    "speed": "1.0000",
                    "in_ts": f"{in_ts:.3f}",
                    "out_ts": f"{out_ts:.3f}",
                    "width": bw,
                    "height": bh,
                    "fps": config.target_fps,
                    "has_audio": 0,
                    "hdr": int(is_hdr),
                    "crf": config.clip_crf,
                    "preset": config.clip_preset,
                    "pan": "none" if pan is None else f"{pan[0]:.4f}-{pan[1]:.4f}",
                },
            )
            if not _link_cached(cache_key, out_path):
                cmd = build_clip_command(
                    source=asset.path,
                    out_path=out_path,
                    in_ts=in_ts,
                    out_ts=out_ts,
                    config=box_config,
                    has_audio=False,  # layers are silent
                    is_hdr=is_hdr,
                    pan=pan,
                )
                async with sem:
                    await asyncio.to_thread(run_ffmpeg, cmd, log_file=log_file)
                _store_cached(cache_key, out_path)
        start, end = window
        return LayerInput(
            path=out_path, start=start, end=end, x=x, y=y, fade=layer_fade(layer, end - start)
        )

    results = await asyncio.gather(*(_one(k, layer) for k, layer in enumerate(layers)))
    rendered = [r for r in results if r is not None]
    if len(rendered) < len(layers):
        log.info("skipped %d B-roll layer(s) outside the program", len(layers) - len(rendered))
    return rendered
