"""Where text and faces can sit without the platform's own UI covering them.

TikTok, Reels and Shorts all draw over a vertical video: tabs and the account
line across the top, the caption, audio line and buttons across the bottom,
and a column of action icons down the right edge. Their published or
measured margins disagree with each other (2025-26 sources), so this is the
UNION: the rectangle none of the three covers, on a 1080x1920 frame.

    x 65-880   (the right rail of icons is the tightest edge)
    y 290-1250 (Reels' bottom UI reaches ~35% of the height)

Other sizes of the same aspect scale proportionally. Landscape and square
output are not overlaid by a feed UI, so they get the conventional 5% title
margins instead.

One definition, shared by the caption layout and the QA scorecard, so the
check and the thing it checks can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

# The union safe rectangle on a 1080x1920 vertical frame.
VERTICAL_REF_W = 1080
VERTICAL_REF_H = 1920
VERTICAL_SAFE = (65, 290, 880, 1250)  # x0, y0, x1, y1
TITLE_SAFE_MARGIN = 0.05


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def contains(self, other: "Rect", tolerance: float = 0.0) -> bool:
        return (
            other.x0 >= self.x0 - tolerance
            and other.y0 >= self.y0 - tolerance
            and other.x1 <= self.x1 + tolerance
            and other.y1 <= self.y1 + tolerance
        )


def is_vertical(width: int, height: int) -> bool:
    return height > width


def safe_rect(width: int, height: int) -> Rect:
    """The rectangle text and faces should stay inside, for this frame size."""
    if is_vertical(width, height):
        sx = width / VERTICAL_REF_W
        sy = height / VERTICAL_REF_H
        x0, y0, x1, y1 = VERTICAL_SAFE
        return Rect(x0 * sx, y0 * sy, x1 * sx, y1 * sy)
    mx = width * TITLE_SAFE_MARGIN
    my = height * TITLE_SAFE_MARGIN
    return Rect(mx, my, width - mx, height - my)
