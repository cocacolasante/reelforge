"""Contact sheets for ranking: three frames per candidate, tiled horizontally.

Frame times: just inside the span start, the energy peak (or midpoint), and
just inside the span end. Tiles are scaled to a fixed HEIGHT — Anthropic
image tokens are ~ width*height/750, so fixed height bounds the cost across
source aspect ratios (a 16:9 sheet lands around 960x180 ≈ 230 tokens; a
360px-WIDE portrait tile would cost ~4x that).

Pure command builder only — extraction (I/O) is orchestrated by
reels/pipeline.py.
"""

from __future__ import annotations

from pathlib import Path

TILE_HEIGHT = 180
# ffmpeg mjpeg quantizer (2-31, lower = better); ~5 corresponds to roughly
# JPEG quality 75 — legible frames at ~20-30 KB per sheet.
JPEG_QSCALE = 5
EDGE_INSET_SEC = 0.5


def sheet_frame_times(
    start: float, end: float, energy_peak_pos: float | None = None
) -> list[float]:
    """[opening, peak-or-midpoint, closing] frame times, all inside the span."""
    dur = max(0.1, end - start)
    inset = min(EDGE_INSET_SEC, dur / 4.0)
    first = start + inset
    last = end - inset
    mid = start + energy_peak_pos * dur if energy_peak_pos is not None else (start + end) / 2.0
    mid = min(max(mid, first), last)
    return [round(first, 3), round(mid, 3), round(last, 3)]


def build_contact_sheet_command(
    source: Path, times: list[float], out_path: Path
) -> list[str]:
    """One ffmpeg invocation: fast-seek to each time, take one frame, scale to
    TILE_HEIGHT, hstack, write JPEG. Pure."""
    args: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    for t in times:
        args += ["-ss", f"{t:.3f}", "-i", str(source)]
    scale_parts = ";".join(
        f"[{i}:v]scale=-2:{TILE_HEIGHT}[t{i}]" for i in range(len(times))
    )
    stack_inputs = "".join(f"[t{i}]" for i in range(len(times)))
    filter_complex = f"{scale_parts};{stack_inputs}hstack=inputs={len(times)}[sheet]"
    args += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[sheet]",
        "-frames:v",
        "1",
        "-q:v",
        str(JPEG_QSCALE),
        str(out_path),
    ]
    return args
