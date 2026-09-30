#!/usr/bin/env bash
# Synthesize 4 small 3D LUTs at Docker build time. Each is a 17-point cube
# that applies a subtle color grade. Generated with Python so we don't ship
# binary .cube files (kept under /app/assets/luts/).
set -euo pipefail
OUT_DIR="${1:-/app/assets/luts}"
mkdir -p "$OUT_DIR"
# The Python half reads OUT; without this it ignored the argument and always
# wrote to /app/assets/luts.
export OUT="$OUT_DIR"

python3 - <<'PY'
import os
from itertools import product
from pathlib import Path

OUT = Path(os.environ.get("OUT", "/app/assets/luts"))
SIZE = 17  # 17^3 = 4913 entries; sub-50 KB each.


def clamp(x):
    return max(0.0, min(1.0, x))


def write_lut(name: str, title: str, transform):
    p = OUT / f"{name}.cube"
    with p.open("w") as f:
        f.write(f"TITLE \"{title}\"\n")
        f.write(f"LUT_3D_SIZE {SIZE}\n")
        # FFmpeg lut3d expects the inner-most axis to be R; outer-most B.
        for b in range(SIZE):
            for g in range(SIZE):
                for r in range(SIZE):
                    rr = r / (SIZE - 1)
                    gg = g / (SIZE - 1)
                    bb = b / (SIZE - 1)
                    out_r, out_g, out_b = transform(rr, gg, bb)
                    f.write(f"{clamp(out_r):.5f} {clamp(out_g):.5f} {clamp(out_b):.5f}\n")
    print(f"wrote {p} ({p.stat().st_size // 1024} KB)")


# Tints are weighted to the MIDTONES (w peaks at 0.5, is 0 at black and
# white): multiplying the whole range pushed highlights past 1.0, where the
# clamp flattened skies and skin into clipped patches.
def _mid(x):
    return 4.0 * x * (1.0 - x)


def _tint(r, g, b, gr, gg, gb):
    return (
        r * (1 + (gr - 1) * _mid(r)),
        g * (1 + (gg - 1) * _mid(g)),
        b * (1 + (gb - 1) * _mid(b)),
    )


def _smoothstep(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


# Warm: lift reds, ease blues back, mostly in the midtones.
def warm(r, g, b):
    return _tint(r, g, b, 1.08, 1.02, 0.95)


# Cool: blue lift, reds eased back.
def cool(r, g, b):
    return _tint(r, g, b, 0.92, 1.00, 1.10)


# Cinematic: teal shadows blending into orange highlights. The blend runs
# across luma 0.25-0.6 — the old version switched hard at 0.4, which drew a
# visible band across any gradient (skies, walls) that crossed it.
def cinematic(r, g, b):
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    k = _smoothstep(0.25, 0.6, luma)
    teal = _tint(r, g, b, 0.95, 1.03, 1.06)
    orange = _tint(r, g, b, 1.08, 1.02, 0.95)
    return tuple(t + (o - t) * k for t, o in zip(teal, orange))


# Vivid: saturation bump via simple distance-from-grey scale.
def vivid(r, g, b):
    grey = (r + g + b) / 3
    factor = 1.18
    return (
        grey + (r - grey) * factor,
        grey + (g - grey) * factor,
        grey + (b - grey) * factor,
    )


write_lut("warm", "ReelForge warm", warm)
write_lut("cool", "ReelForge cool", cool)
write_lut("cinematic", "ReelForge cinematic teal/orange", cinematic)
write_lut("vivid", "ReelForge vivid saturation", vivid)
PY

echo "LUT synthesis complete: $(ls "$OUT_DIR"/*.cube | wc -l) files"
