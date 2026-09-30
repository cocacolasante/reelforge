"""Turn a media file already on disk into an Asset row.

Shared by the phone upload page and the watch folder. The chunked upload
route does the same work inline against its assembled temp file; the rules
that matter are here, in one place: the id is the content hash (so the same
footage submitted twice reuses the analysis it already paid for), the file
lands under `/data/uploads/{asset_id}.{ext}`, and an unreadable file is
rejected with something the user can act on.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod
from apps.api.settings import settings
from reelforge_core.ingest import asset_to_dict, probe

log = logging.getLogger(__name__)


class UnreadableMedia(Exception):
    """The file isn't media this FFmpeg can decode."""

    def __init__(self, filename: str, detail: str):
        self.filename = filename
        self.detail = detail
        suffix = Path(filename).suffix.lower().lstrip(".")
        hint = (
            " iPhone HEIC photos aren't supported yet — in Photos choose "
            "File -> Export -> Export Photo and pick JPEG, or set Settings -> "
            "Camera -> Formats -> Most Compatible."
            if suffix in {"heic", "heif"}
            else ""
        )
        super().__init__(f"could not read {filename!r} as media.{hint}")


def _final_path(asset_id: str, filename: str) -> Path:
    ext = Path(filename).suffix.lstrip(".").lower() or "mp4"
    return settings.data_dir / "uploads" / f"{asset_id}.{ext}"


async def ingest_media_file(
    db: AsyncSession,
    project_id: str,
    source: Path,
    original_filename: str,
    *,
    move: bool,
) -> tuple[dbmod.Asset, bool]:
    """Probe `source`, file it under its content id, and return
    `(asset, created)`.

    `move=True` consumes the file (an upload's temp file); `move=False` copies
    it (the watch folder, where the original is the user's own).

    An asset id IS the content hash, and an asset belongs to one project, so
    submitting footage that is already here returns the existing row with
    `created=False` — possibly pointing at a DIFFERENT project. Callers must
    say so rather than reporting a clip into a batch that never received it.
    """
    try:
        probed = probe(source)
    except Exception as exc:  # noqa: BLE001 — every failure means "not media"
        raise UnreadableMedia(original_filename, str(exc)[:200]) from exc

    final_path = _final_path(probed.id, original_filename)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        # Same content already here: keep the copy we know probed cleanly.
        if move:
            source.unlink(missing_ok=True)
    elif move:
        source.replace(final_path)
    else:
        shutil.copy2(source, final_path)
    # Re-probe at the durable path so probe_json references where it lives.
    asset = probe(final_path)

    existing = await db.get(dbmod.Asset, asset.id)
    if existing is not None:
        return existing, False

    row = dbmod.Asset(
        id=asset.id,
        project_id=project_id,
        kind="audio" if asset.is_audio else ("photo" if asset.is_photo else "video"),
        path=str(asset.path),
        original_filename=original_filename,
        duration_sec=asset.probe.duration_s,
        width=asset.probe.width,
        height=asset.probe.height,
        fps=asset.probe.fps,
        has_audio=asset.has_audio,
        size_bytes=asset.size_bytes,
        probe_json=json.dumps(asset_to_dict(asset)),
    )
    db.add(row)
    project = await db.get(dbmod.Project, project_id)
    if project is not None and project.source_asset_id is None:
        project.source_asset_id = asset.id
    await db.commit()
    await db.refresh(row)
    return row, True
