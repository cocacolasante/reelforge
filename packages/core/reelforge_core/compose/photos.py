"""Render still photos into normalized video shots.

A photo becomes a clip that looks and behaves exactly like an extracted
video clip — same resolution, fps, pixel format and a silent 48 kHz stereo
track — so the xfade/acrossfade chain in `graph_builder` can treat the two
identically.

Motion is baked in here rather than in the render graph: a still frame held
for three seconds reads as a glitch, so the crop window drifts across the
image (the same scale + animated-crop trick Ken Burns uses, which is far
cheaper than zoompan). Photos therefore never take the graph's Ken Burns
path — see `graph_builder`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from reelforge_core.compose.graph import run_ffmpeg
from reelforge_core.models import ComposeConfig, PhotoInsert

log = logging.getLogger(__name__)

# How far the crop window travels across the image, as a fraction of the
# available margin. Subtle on purpose.
PAN_ZOOM = 1.12


def build_photo_clip_command(
    *,
    source: Path,
    out_path: Path,
    duration_sec: float,
    config: ComposeConfig,
    ken_burns: bool = True,
    pan_index: int = 0,
) -> list[str]:
    """FFmpeg argv turning one still into a normalized clip.

    `pan_index` alternates the drift direction so consecutive photos don't
    all move the same way.
    """
    width, height = config.resolution
    fps = config.target_fps
    dur = max(0.2, duration_sec)

    if ken_burns:
        # Fill the frame at PAN_ZOOM, then slide a target-sized window across
        # the surplus. force_original_aspect_ratio=increase guarantees the
        # scaled image covers the crop on both axes whatever the photo's
        # shape, so there are never black edges.
        sw = int(width * PAN_ZOOM / 2) * 2
        sh = int(height * PAN_ZOOM / 2) * 2
        progress = f"min(t/{dur:.3f}\\,1)"
        # Four directions, rotating: →, ←, ↓, ↑
        direction = pan_index % 4
        if direction == 0:
            x_expr, y_expr = f"(iw-ow)*{progress}", "(ih-oh)/2"
        elif direction == 1:
            x_expr, y_expr = f"(iw-ow)*(1-{progress})", "(ih-oh)/2"
        elif direction == 2:
            x_expr, y_expr = "(iw-ow)/2", f"(ih-oh)*{progress}"
        else:
            x_expr, y_expr = "(iw-ow)/2", f"(ih-oh)*(1-{progress})"
        vf = (
            f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:x='{x_expr}':y='{y_expr}',"
            f"setsar=1,fps={fps},format=yuv420p"
        )
    else:
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format=yuv420p"
        )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{dur:.3f}",
        "-i",
        str(source),
        # Silent bed so every shot in the chain carries an audio stream.
        "-f",
        "lavfi",
        "-t",
        f"{dur:.3f}",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        config.clip_preset,
        "-crf",
        str(config.clip_crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{config.audio_bitrate_kbps}k",
        "-movflags",
        "+faststart",
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-shortest",
        str(out_path),
    ]


async def render_photo_clip(
    insert: PhotoInsert,
    out_path: Path,
    config: ComposeConfig,
    log_file: Path,
    pan_index: int = 0,
) -> Path:
    """Render one photo shot, reusing the content cache across composes."""
    import shutil as _shutil

    from reelforge_core import cache as file_cache

    source = Path(insert.path)
    if not source.exists():
        raise FileNotFoundError(f"photo not found: {source}")

    w, h = config.resolution
    cache_key = file_cache.compute_key(
        "clip",
        {
            "photo": str(source),
            "mtime": int(source.stat().st_mtime),
            "dur": f"{insert.duration_sec:.3f}",
            "width": w,
            "height": h,
            "fps": config.target_fps,
            "ken_burns": int(insert.ken_burns),
            "pan": pan_index % 4,
            "crf": config.clip_crf,
            "preset": config.clip_preset,
        },
    )
    cached = file_cache.lookup(cache_key)
    if cached is not None:
        try:
            if out_path.exists():
                out_path.unlink()
            import os as _os

            _os.link(cached, out_path)
        except OSError:
            _shutil.copy2(cached, out_path)
        return out_path

    cmd = build_photo_clip_command(
        source=source,
        out_path=out_path,
        duration_sec=insert.duration_sec,
        config=config,
        ken_burns=insert.ken_burns,
        pan_index=pan_index,
    )
    await asyncio.to_thread(run_ffmpeg, cmd, log_file=log_file)
    try:
        cache_target = file_cache.path_for("clip", cache_key, "mp4")
        _shutil.copy2(out_path, cache_target)
        file_cache.register(cache_key, "clip", cache_target)
        file_cache.evict_if_over_cap("clip", file_cache.cap_from_env("clip", 20.0))
    except Exception:  # pragma: no cover
        log.exception("photo clip cache write failed for %s", cache_key)
    return out_path


def interleave_photo_clips(
    video_clips: list,
    photo_clips: list[tuple[int, object]],
) -> list:
    """Merge photo shots into the video shot list at their positions.

    `photo_clips` is [(position, clip)]: position 0 puts the photo before the
    first video clip, N after the Nth, and anything >= the clip count lands
    at the end. Photos sharing a position keep the caller's order.
    """
    n = len(video_clips)
    by_position: dict[int, list] = {}
    for position, clip in photo_clips:
        idx = max(0, min(int(position), n))
        by_position.setdefault(idx, []).append(clip)

    out: list = []
    for i, video_clip in enumerate(video_clips):
        out.extend(by_position.get(i, []))
        out.append(video_clip)
    out.extend(by_position.get(n, []))
    return out
