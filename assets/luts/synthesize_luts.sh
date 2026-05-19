#!/usr/bin/env bash
# Synthesize 4 small 3D LUTs at Docker build time. Each is a 17-point cube
# that applies a subtle color grade. Generated with Python so we don't ship
# binary .cube files (kept under /app/assets/luts/).
set -euo pipefail
OUT_DIR="${1:-/app/assets/luts}"
mkdir -p "$OUT_DIR"

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


# Warm: lift reds + small green/blue retreat in shadows; gentle midtone warmth.
def warm(r, g, b):
    return (r * 1.08, g * 1.02, b * 0.95)


# Cool: blue lift + slight desaturation in highlights.
def cool(r, g, b):
    return (r * 0.92, g * 1.00, b * 1.10)


# Cinematic: teal-shadow + orange-highlight (subtle).
def cinematic(r, g, b):
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    if luma < 0.4:
        # Push shadows toward teal
        return (r * 0.95, g * 1.03, b * 1.06)
    # Lift highlights toward orange
    return (r * 1.08, g * 1.02, b * 0.95)


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
