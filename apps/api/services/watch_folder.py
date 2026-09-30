"""Ingest footage dropped into a synced folder.

Point `REELFORGE_WATCH_DIR` at an iCloud or Dropbox folder and clips saved
there from a phone become projects here, with no tapping and no upload link.

Two rules keep it predictable:

  * A file is taken only once it has stopped changing (`watch_settle_seconds`)
    — a clip still syncing would otherwise be probed half-written.
  * A subfolder becomes its own project, named after the folder; loose files
    at the top level join a project named for the day they arrived. Dropping
    a shoot's folder in therefore produces exactly one project to cut from.

The original file is never moved or deleted: it is the user's copy, and the
`watch_ingests` table is what stops it being ingested twice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api import db as dbmod
from apps.api.services.ingest_file import UnreadableMedia, ingest_media_file
from apps.api.settings import settings

log = logging.getLogger(__name__)

# What a phone or camera produces. Anything else in the folder is ignored
# rather than probed — a stray .txt shouldn't show up as a failed ingest.
MEDIA_SUFFIXES = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",  # video
    ".jpg", ".jpeg", ".png", ".heic", ".heif",        # stills
}
MAX_PER_SCAN = 25  # ingest is CPU-bound (ffprobe); don't monopolise a scan


def watch_dir() -> Path | None:
    raw = (settings.watch_dir or "").strip()
    return Path(raw) if raw else None


def _candidates(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES and not path.name.startswith("."):
            out.append(path)
    return out


def _settled(path: Path, now: float) -> bool:
    try:
        return (now - path.stat().st_mtime) >= settings.watch_settle_seconds
    except OSError:
        return False


def project_name_for(path: Path, root: Path) -> str:
    """A subfolder names its own project; loose files join the day's."""
    rel = path.relative_to(root)
    if len(rel.parts) > 1:
        return rel.parts[0]
    return f"Dropped in {date.today().isoformat()}"


async def _project_id(db: AsyncSession, name: str) -> str:
    existing = (
        await db.execute(select(dbmod.Project).where(dbmod.Project.name == name))
    ).scalars().first()
    if existing is not None:
        return existing.id
    project = dbmod.Project(name=name)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    log.info("watch folder: created project %r", name)
    return project.id


async def _already_ingested(db: AsyncSession, path: Path, stat_size: int, mtime: float) -> bool:
    rows = (
        await db.execute(
            select(dbmod.WatchIngest).where(dbmod.WatchIngest.path == str(path))
        )
    ).scalars().all()
    return any(r.size_bytes == stat_size and abs(r.mtime - mtime) < 1.0 for r in rows)


async def scan_once(db: AsyncSession) -> int:
    """Ingest everything settled and new. Returns how many files were taken."""
    root = watch_dir()
    if root is None or not root.is_dir():
        return 0
    now = time.time()
    taken = 0
    for path in _candidates(root):
        if taken >= MAX_PER_SCAN:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        if not _settled(path, now):
            continue
        if await _already_ingested(db, path, stat.st_size, stat.st_mtime):
            continue

        record = dbmod.WatchIngest(
            path=str(path), size_bytes=stat.st_size, mtime=stat.st_mtime
        )
        try:
            project_id = await _project_id(db, project_name_for(path, root))
            asset, created = await ingest_media_file(
                db, project_id, path, path.name, move=False
            )
            record.asset_id = asset.id
            record.project_id = asset.project_id
            taken += 1
            log.info(
                "watch folder: %s %s -> %s",
                "ingested" if created else "already had", path.name, asset.project_id,
            )
        except UnreadableMedia as exc:
            # Remembered as a failure so the next scan doesn't retry it
            # forever; a re-saved file has a new mtime and will be retried.
            record.error = str(exc)[:300]
            log.warning("watch folder: %s", exc)
        except Exception as exc:  # noqa: BLE001 — one bad file must not stop the scan
            record.error = str(exc)[:300]
            log.exception("watch folder: failed on %s", path)
        db.add(record)
        await db.commit()
    return taken


async def watch_loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Background scan; a failure sleeps and retries rather than dying."""
    root = watch_dir()
    if root is None:
        log.info("watch folder: disabled (REELFORGE_WATCH_DIR unset)")
        return
    log.info("watch folder: watching %s", root)
    while True:
        try:
            async with sessionmaker() as db:
                await scan_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("watch folder: scan failed")
        await asyncio.sleep(settings.watch_scan_seconds)
