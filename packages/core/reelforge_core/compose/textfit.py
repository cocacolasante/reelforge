"""Measuring and fitting burned-in text to the safe area.

One measurement for both sides: caption layout uses it to break lines so
nothing runs past the platform-safe rectangle, and the QA scorecard uses the
same numbers to check the result. Measured with the real font via PIL when it
can be found; otherwise estimated from the size, deliberately on the wide
side so an uncertain line gets broken rather than overflowing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_EST_CHAR_WIDTH = 0.62  # bold caption faces average ~0.58 of their size
_FONT_DIRS = (
    Path("/app/assets/fonts"),
    Path("/usr/share/fonts/opentype"),
    Path("/usr/share/fonts/truetype"),
)


@lru_cache(maxsize=32)
def font_path(family: str, bold: bool) -> Path | None:
    """The font file libass would pick for this family, best effort. A family
    that names its weight ("Montserrat Black") matches that file directly."""
    want = family.replace(" ", "").lower()
    candidates: list[Path] = []
    for root in _FONT_DIRS:
        if root.is_dir():
            candidates.extend(p for p in root.rglob("*") if p.suffix.lower() in {".ttf", ".otf"})

    def key(p: Path) -> str:
        return p.stem.replace("-", "").replace(" ", "").lower()

    exact = [p for p in candidates if key(p) == want]
    if exact:
        return exact[0]
    named = [p for p in candidates if key(p).startswith(want) and "italic" not in key(p)]
    if not named:
        return None
    weight = "bold" if bold else "regular"
    for p in named:
        if key(p).endswith(weight):
            return p
    return named[0]


@lru_cache(maxsize=256)
def _pil_font(path: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


@lru_cache(maxsize=4096)
def text_width(text: str, family: str, size: float, bold: bool = False) -> float:
    path = font_path(family, bold)
    if path is not None:
        try:
            return float(_pil_font(str(path), max(1, int(round(size)))).getlength(text))
        except Exception:  # noqa: BLE001 — fall back to the estimate
            pass
    return len(text) * size * _EST_CHAR_WIDTH


def wrap_to_width(
    words: list[str], family: str, size: float, max_width: float, *, bold: bool = False
) -> list[str]:
    """Greedy line breaking so no line is wider than `max_width`. A single
    word wider than the limit gets a line to itself (callers shrink it)."""
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join([*current, word])
        if current and text_width(trial, family, size, bold) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def fit_size(text: str, family: str, size: float, max_width: float, *, bold: bool = False) -> float:
    """The largest size <= `size` at which `text` fits on one line."""
    width = text_width(text, family, size, bold)
    if width <= max_width or width <= 0:
        return size
    return max(12.0, size * max_width / width * 0.98)
