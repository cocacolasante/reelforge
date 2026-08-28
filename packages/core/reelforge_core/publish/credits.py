"""Music attribution for published reels.

CC-BY tracks legally require a credit line wherever the video is posted.
`music_credit_for_reel` reads the reel's compose.json and returns the credit
line when (and only when) the chosen track's license requires attribution;
`append_credit` idempotently appends it to a description/caption.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Licenses whose terms require attribution in the posted description/caption.
ATTRIBUTION_REQUIRED_PREFIXES = ("CC-BY",)


def music_credit_for_reel(asset_id: str, reel_id: str, data_dir: Path) -> str | None:
    """Credit line for the reel's rendered music, or None (no music / no
    attribution required / manifest unreadable — never raises)."""
    manifest = data_dir / "working" / asset_id / "reels" / reel_id / "compose.json"
    try:
        data = json.loads(manifest.read_text())
    except Exception:
        return None
    track = data.get("chosen_music")
    if not track:
        return None
    license_id = (track.get("license") or "").upper()
    attribution = track.get("attribution")
    if not attribution:
        return None
    if not license_id.startswith(ATTRIBUTION_REQUIRED_PREFIXES):
        return None
    return attribution if attribution.lower().startswith("music") else f"Music: {attribution}"


def append_credit(text: str, credit: str | None) -> str:
    """Append the credit on its own paragraph; no-op if already present."""
    if not credit or credit in text:
        return text
    return f"{text}\n\n{credit}" if text.strip() else credit
