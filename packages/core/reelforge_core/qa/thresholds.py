"""What "professional" means, as numbers, per kind of reel.

Sources (2025-26, see docs/editing-quality.md for the research brief):
talking-head visual change every ~2-4s; captions of 1-3 words with only key
words highlighted; the first word within half a second and no greeting;
-14 LUFS integrated with a -1 dBTP ceiling; an ending that lands rather than
trails off. These are the targets each checkpoint is judged against — change
them deliberately, since a moved goalpost hides a regression.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    metric: str
    op: str  # ">=", "<=", "==", "between"
    value: float | tuple[float, float]
    note: str = ""

    def check(self, measured) -> str:
        """pass | fail | n/a"""
        if measured is None:
            return "n/a"
        if self.op == ">=":
            return "pass" if measured >= self.value else "fail"
        if self.op == "<=":
            return "pass" if measured <= self.value else "fail"
        if self.op == "==":
            return "pass" if measured == self.value else "fail"
        if self.op == "between":
            lo, hi = self.value  # type: ignore[misc]
            return "pass" if lo <= measured <= hi else "fail"
        raise ValueError(f"unknown op {self.op}")


COMMON = [
    Target("extra_flashy_transitions", "==", 0, "pros cut: one slide/dip per reel, 10% on long ones"),
    Target("safe_zone_violations", "==", 0, "captions and overlays clear of platform UI"),
    Target("words_per_caption_p95", "<=", 3, "1-3 word chunks"),
    Target("highlighted_share", "between", (0.10, 0.25), "highlight key words only"),
    Target("integrated_lufs", "between", (-15.0, -13.0), "-14 LUFS"),
    Target("true_peak_dbtp", "<=", -1.0, "-1 dBTP ceiling"),
    Target("sfx_per_min", "<=", 10.0, "sound effects sparse: one per 6s at most"),
    Target("face_in_crop", ">=", 0.95, "the tracked face stays in the reframed crop"),
]

SPEECH = [
    Target("hook_latency_sec", "<=", 0.5, "first word within half a second"),
    Target("greeting", "==", 0, "no 'hey guys' / 'welcome back' opener"),
    Target("dead_air_longest_sec", "<=", 0.4, "no stall over 0.4s"),
    Target("dead_air_percent", "<=", 3.0, "stalls under 3% of speech"),
    Target("fillers_per_min", "<=", 2.0),
    Target("trailing_filler", "==", 0, "no 'so yeah' ending"),
]

BY_KIND: dict[str, list[Target]] = {
    "talking": [
        Target("changes_per_min", ">=", 15, "a new visual every ~4s or sooner"),
        Target("longest_static_sec", "<=", 4.0),
        *SPEECH,
        *COMMON,
    ],
    "action": [
        Target("changes_per_min", ">=", 20),
        Target("longest_static_sec", "<=", 3.5, "payoff shot may run longer"),
        *COMMON,
    ],
    "long_form": [
        Target("changes_per_min", ">=", 7.5),
        Target("longest_static_sec", "<=", 8.0),
        *SPEECH,
        *COMMON,
    ],
}
