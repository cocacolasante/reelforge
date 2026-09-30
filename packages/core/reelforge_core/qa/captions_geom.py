"""Where each caption actually lands on screen, read back from captions.ass.

Reading the rendered subtitle file rather than the config means the check
covers whatever produced the events — spoken captions, editor overlays, the
director's hook text — including inline overrides (`\\an`, `\\pos`, `\\fs`).

Text width is measured with the real font when PIL can find it and
estimated from the font size otherwise; the estimate is deliberately wide,
so an uncertain caption is reported as a violation rather than waved through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from reelforge_core.compose.safezone import Rect
from reelforge_core.compose.textfit import text_width

_LINE_HEIGHT = 1.2

_OVERRIDE_BLOCK = re.compile(r"\{[^}]*\}")
_AN = re.compile(r"\\an([1-9])")
_POS = re.compile(r"\\pos\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
_FS = re.compile(r"\\fs([\d.]+)")
# Inline colour overrides: {\c&HBBGGRR&...}. A highlighted word is text set in
# the style's highlight colour (its SecondaryColour), up to the next colour
# change or reset — karaoke closes with {\r}, punch restores the primary.
_COLOR_RUN = re.compile(r"\{([^}]*)\}([^{]*)")
_INLINE_C = re.compile(r"\\c&H([0-9A-Fa-f]+)&")


@dataclass
class AssStyle:
    name: str
    font: str
    size: float
    bold: bool
    outline: float
    shadow: float
    alignment: int
    margin_l: float
    margin_r: float
    margin_v: float
    highlight: str = ""  # SecondaryColour as inline BBGGRR — our highlight colour


@dataclass
class CaptionBox:
    start: float
    end: float
    style: str
    text: str  # plain text, lines joined with a space
    lines: int
    words: int
    highlighted: list[str] = field(default_factory=list)
    rect: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))


@dataclass
class AssDocument:
    width: int
    height: int
    boxes: list[CaptionBox]


def _ts(value: str) -> float:
    h, m, s = value.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _fields(line: str, fmt: list[str]) -> dict[str, str]:
    # The last field (Text) may itself contain commas.
    parts = line.split(",", len(fmt) - 1)
    return {k: v.strip() if k != "text" else v for k, v in zip(fmt, parts)}


def _box_for(
    lines: list[str],
    style: AssStyle,
    width: int,
    height: int,
    alignment: int,
    size: float,
    pos: tuple[float, float] | None,
    margins: tuple[float, float, float],
) -> Rect:
    ml, mr, mv = margins
    pad = style.outline + style.shadow
    text_w = max((text_width(ln, style.font, size, style.bold) for ln in lines), default=0.0) + 2 * pad
    text_h = len(lines) * size * _LINE_HEIGHT + 2 * pad
    column = (alignment - 1) % 3  # 0 left, 1 centre, 2 right
    row = (alignment - 1) // 3  # 0 bottom, 1 middle, 2 top
    if pos is not None:
        ax, ay = pos
    else:
        ax = ml if column == 0 else (width - mr if column == 2 else (width + ml - mr) / 2.0)
        ay = height - mv if row == 0 else (mv if row == 2 else height / 2.0)
    x0 = ax if column == 0 else (ax - text_w if column == 2 else ax - text_w / 2.0)
    y0 = ay - text_h if row == 0 else (ay if row == 2 else ay - text_h / 2.0)
    return Rect(x0, y0, x0 + text_w, y0 + text_h)


def _highlight_colour(style_fields: dict[str, str]) -> str:
    """The style's SecondaryColour as inline BBGGRR — our highlight — unless
    it matches the normal text colour, in which case nothing can stand out
    (the Overlay style is white on white)."""
    def bgr(key: str) -> str:
        return style_fields.get(key, "").removeprefix("&H")[-6:].upper()

    secondary, primary = bgr("secondarycolour"), bgr("primarycolour")
    return "" if not secondary or secondary == primary else secondary


def _highlighted_words(text: str, highlight: str) -> list[str]:
    """Words set in the highlight colour. Walks the event's override blocks:
    a `\\c` equal to the highlight turns highlighting on, any other `\\c` or
    a `\\r` turns it off. Overlays (whose colour is white) never count."""
    if not highlight:
        return []
    out: list[str] = []
    on = False
    for tags, run in _COLOR_RUN.findall("{}" + text):
        colours = _INLINE_C.findall(tags)
        if colours:
            on = colours[-1].upper()[-6:] == highlight
        if "\\r" in tags:
            on = False
        if on:
            out.extend(w for w in run.replace("\\N", " ").split() if w)
    return out


def parse_ass(path: Path) -> AssDocument:
    """Every Dialogue event as an on-screen box."""
    width, height = 1080, 1920
    styles: dict[str, AssStyle] = {}
    style_fmt: list[str] = []
    event_fmt: list[str] = []
    section = ""
    raw_events: list[dict[str, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if section == "[script info]":
            if key == "playresx":
                width = int(float(value))
            elif key == "playresy":
                height = int(float(value))
        elif section == "[v4+ styles]":
            if key == "format":
                style_fmt = [f.strip().lower() for f in value.split(",")]
            elif key == "style" and style_fmt:
                f = _fields(value.strip(), style_fmt)
                styles[f["name"]] = AssStyle(
                    name=f["name"],
                    font=f.get("fontname", "Inter"),
                    size=float(f.get("fontsize", 48)),
                    bold=f.get("bold", "0") not in ("0", ""),
                    outline=float(f.get("outline", 0) or 0),
                    shadow=float(f.get("shadow", 0) or 0),
                    alignment=int(float(f.get("alignment", 2) or 2)),
                    margin_l=float(f.get("marginl", 0) or 0),
                    margin_r=float(f.get("marginr", 0) or 0),
                    margin_v=float(f.get("marginv", 0) or 0),
                    highlight=_highlight_colour(f),
                )
        elif section == "[events]":
            if key == "format":
                event_fmt = [f.strip().lower() for f in value.split(",")]
            elif key == "dialogue" and event_fmt:
                raw_events.append(_fields(value.lstrip(), event_fmt))

    default = next(iter(styles.values()), None)
    boxes: list[CaptionBox] = []
    for ev in raw_events:
        style = styles.get(ev.get("style", ""), default)
        if style is None:
            continue
        text = ev.get("text", "")
        tags = " ".join(_OVERRIDE_BLOCK.findall(text))
        an = _AN.search(tags)
        pos = _POS.search(tags)
        fs = _FS.search(tags)
        highlighted = _highlighted_words(text, style.highlight)
        plain = _OVERRIDE_BLOCK.sub("", text).replace("\\h", " ")
        lines = [ln.strip() for ln in plain.split("\\N")]
        lines = [ln for ln in lines if ln] or [""]
        margins = (
            float(ev.get("marginl") or 0) or style.margin_l,
            float(ev.get("marginr") or 0) or style.margin_r,
            float(ev.get("marginv") or 0) or style.margin_v,
        )
        rect = _box_for(
            lines,
            style,
            width,
            height,
            int(an.group(1)) if an else style.alignment,
            float(fs.group(1)) if fs else style.size,
            (float(pos.group(1)), float(pos.group(2))) if pos else None,
            margins,
        )
        joined = " ".join(lines)
        boxes.append(
            CaptionBox(
                start=_ts(ev.get("start", "0:00:00.00")),
                end=_ts(ev.get("end", "0:00:00.00")),
                style=style.name,
                text=joined,
                lines=len([ln for ln in lines if ln]),
                words=len(joined.split()),
                highlighted=highlighted,
                rect=rect,
            )
        )
    return AssDocument(width=width, height=height, boxes=boxes)
