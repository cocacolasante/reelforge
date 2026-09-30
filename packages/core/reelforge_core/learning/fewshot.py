"""The creator's best performers as ranking examples (CP12).

When enough labelled clips exist, the top performers (by completion, shares
breaking ties) are described in the ranking prompt: what they were, how long,
how they opened. The ranker then leans toward what THIS audience finished.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from reelforge_core.learning.dataset import Row, collect

MIN_LABELS_FOR_EXAMPLES = 10
EXAMPLES = 4


def top_examples(rows: list[Row], k: int = EXAMPLES) -> list[Row]:
    """The best performers that we can describe (joined to their reel).
    Pure."""
    usable = [r for r in rows if r.reel and r.targets.get("completion_rate") is not None]
    if len(usable) < MIN_LABELS_FOR_EXAMPLES:
        return []
    usable.sort(key=lambda r: (-(r.targets["completion_rate"] or 0.0),
                               -(r.targets.get("shares_per_view") or 0.0)))
    return usable[:k]


def examples_block(rows: list[Row]) -> str | None:
    """The system-prompt block, or None. Pure."""
    best = top_examples(rows)
    if len(best) < 3:
        return None
    lines = ["\n\nWHAT HAS WORKED FOR THIS CREATOR\n"
             "These reels of theirs held viewers best. Treat them as evidence of "
             "what this audience finishes and shares — not as templates to copy:"]
    for r in best:
        reel = r.reel
        comp = r.targets["completion_rate"]
        shares = r.targets.get("shares_per_view")
        lines.append(
            f"- \"{reel.get('title', '')}\" — {reel.get('duration_sec', 0):.0f}s, "
            f"{reel.get('edit_style') or 'unclassified'}; opened on: "
            f"{reel.get('opening_description') or reel.get('hook') or 'n/a'}; "
            f"completion {comp:.0%}" + (f", shares/view {shares:.2%}" if shares is not None else "")
        )
    return "\n".join(lines)


_CACHE: dict[str, tuple[float, str | None]] = {}


def active_block(data_dir: Path) -> str | None:
    """The examples block for the live label set, cached on the labels
    database's mtime. None until enough labels exist."""
    from reelforge_core.learning.dataset import labels_db

    db = labels_db(data_dir)
    try:
        mtime = db.stat().st_mtime
    except OSError:
        return None
    key = str(db)
    if key in _CACHE and _CACHE[key][0] == mtime:
        return _CACHE[key][1]
    block = examples_block(collect(data_dir))
    _CACHE[key] = (mtime, block)
    return block


def block_digest(block: str | None) -> str | None:
    return hashlib.sha1(block.encode()).hexdigest()[:10] if block else None
