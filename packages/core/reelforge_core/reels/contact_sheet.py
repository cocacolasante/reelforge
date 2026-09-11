"""Frame sheets for the models that pick and trim reels (pure command builders).

Contact sheet — ranking and AI-mix sequencing: FIVE frames tiled horizontally:
2s BEFORE the span, just inside its start, the energy peak (or midpoint), just
inside its end, 2s AFTER the span. The two outer frames are OUTSIDE the clip and
carry a red border, so the model can see what a cut leaves out — the wave
arriving right after the end, the fall right before the start. (The original
three-inside-frames sheet gave it no way to see a missing payoff.)

Edge strip — boundary refinement: EIGHT frames around the current bounds: 3s and
1.5s before the start, the start, 1.5s in | 1.5s before the end, the end, 1.5s
and 3s after — outside frames red-bordered.

Times past either edge of the footage render as black tiles. Tiles are scaled
to a fixed HEIGHT — Anthropic image tokens are ~ width*height/750, so fixed
height bounds the cost across source aspect ratios (16:9: a sheet is 1600x180
≈ 380 tokens, a strip 2560x180 ≈ 610).

Extraction (I/O) is orchestrated by reels/pipeline.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

TILE_HEIGHT = 180
# Black stand-in tile for times past the footage edges (16:9 at TILE_HEIGHT).
BLANK_TILE_WIDTH = 320
# ffmpeg mjpeg quantizer (2-31, lower = better); ~5 corresponds to roughly
# JPEG quality 75 — legible frames at ~20-30 KB per sheet.
JPEG_QSCALE = 5
EDGE_INSET_SEC = 0.5
OUTSIDE_OFFSET_SEC = 2.0
OUTSIDE_BORDER = "drawbox=x=0:y=0:w=iw:h=ih:color=red:t=8"
# Part of every cached sheet/strip filename — bump when a layout changes so a
# stale layout is never reused.
SHEET_VERSION = "s2"
# Tile indices that lie OUTSIDE the span, per layout.
SHEET_OUTSIDE = (0, 4)
STRIP_OUTSIDE = (0, 1, 6, 7)
_FOOTAGE_EDGE_EPS = 0.05


def _outside(t: float, duration: float | None) -> float | None:
    if t < 0.0 or (duration is not None and t > duration - _FOOTAGE_EDGE_EPS):
        return None
    return round(t, 3)


def sheet_frame_times(
    start: float,
    end: float,
    energy_peak_pos: float | None = None,
    duration: float | None = None,
) -> list[float | None]:
    """[before, opening, peak-or-midpoint, closing, after]. The inner three sit
    inside the span; the outer two lie OUTSIDE_OFFSET_SEC past its edges (None
    when that is past the footage edge)."""
    dur = max(0.1, end - start)
    inset = min(EDGE_INSET_SEC, dur / 4.0)
    first = start + inset
    last = end - inset
    mid = start + energy_peak_pos * dur if energy_peak_pos is not None else (start + end) / 2.0
    mid = min(max(mid, first), last)
    return [
        _outside(start - OUTSIDE_OFFSET_SEC, duration),
        round(first, 3),
        round(mid, 3),
        round(last, 3),
        _outside(end + OUTSIDE_OFFSET_SEC, duration),
    ]


def edge_strip_times(
    start: float, end: float, duration: float | None = None
) -> list[float | None]:
    """[start-3, start-1.5, start, start+1.5, end-1.5, end, end+1.5, end+3]; the
    four inside frames are kept inside the span."""
    inset = min(0.1, (end - start) / 4.0)
    first, last = start + inset, end - inset
    return [
        _outside(start - 3.0, duration),
        _outside(start - 1.5, duration),
        round(first, 3),
        round(min(start + 1.5, last), 3),
        round(max(end - 1.5, first), 3),
        round(last, 3),
        _outside(end + 1.5, duration),
        _outside(end + 3.0, duration),
    ]


def build_contact_sheet_command(
    source: Path,
    times: Sequence[float | None],
    out_path: Path,
    *,
    outside: Sequence[int] = (),
) -> list[str]:
    """One ffmpeg invocation: fast-seek to each time and take one frame (a black
    tile for None), scale to TILE_HEIGHT, red-border the `outside` tiles,
    hstack, write JPEG. Pure."""
    args: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    for t in times:
        if t is None:
            args += ["-f", "lavfi", "-i", f"color=c=black:s={BLANK_TILE_WIDTH}x{TILE_HEIGHT}:d=1"]
        else:
            args += ["-ss", f"{t:.3f}", "-i", str(source)]
    chains: list[str] = []
    for i in range(len(times)):
        # format=yuv420p: black lavfi tiles and 10-bit sources must share a
        # pixel format for hstack.
        chain = f"[{i}:v]scale=-2:{TILE_HEIGHT},format=yuv420p"
        if i in outside:
            chain += f",{OUTSIDE_BORDER}"
        chains.append(f"{chain}[t{i}]")
    stack_inputs = "".join(f"[t{i}]" for i in range(len(times)))
    filter_complex = ";".join(chains) + f";{stack_inputs}hstack=inputs={len(times)}[sheet]"
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
