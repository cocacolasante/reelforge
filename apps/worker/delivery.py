"""Get finished clips to the person who asked for them.

Three routes, chosen per request:

  links   signed, expiring URLs on our own origin (always available)
  folder  copied into a synced folder, so they appear in Files on a phone
  email   a message listing the clips, with those same links

A delivery failure never fails the cut: the clips exist and are listed
either way, and each channel reports its own outcome so the agent can say
"they're rendered, but the email didn't go" instead of claiming success.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import smtplib
from email.message import EmailMessage
from pathlib import Path

from reelforge_core import links

log = logging.getLogger(__name__)

CHANNELS = ("links", "folder", "email")
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")


def parse_channels(raw: object, default: str = "links") -> list[str]:
    """Accepts a list or a comma-separated string; unknown names are dropped."""
    if isinstance(raw, str):
        wanted = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        wanted = [str(p).strip() for p in raw]
    else:
        wanted = []
    picked = [c for c in wanted if c in CHANNELS]
    if not picked:
        picked = [c for c in (p.strip() for p in default.split(",")) if c in CHANNELS]
    # Stable order, no duplicates.
    return [c for c in CHANNELS if c in picked]


def safe_filename(title: str, fallback: str) -> str:
    cleaned = _UNSAFE.sub(" ", title or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:70]
    return f"{cleaned}.mp4" if cleaned else fallback


def _public_base() -> str:
    return (
        os.environ.get("REELFORGE_PUBLIC_MEDIA_BASE")
        or os.environ.get("REELFORGE_PUBLIC_API_BASE")
        or "http://localhost:8001"
    )


def add_links(clips: list[dict]) -> None:
    """Attach a signed URL to each clip, in place."""
    base = _public_base()
    for clip in clips:
        path = clip.get("exportPath")
        if not path:
            continue
        try:
            clip["url"] = links.media_url(path, base)
            clip["urlExpiresInHours"] = round(links.MEDIA_LINK_TTL_S / 3600)
        except Exception as exc:  # noqa: BLE001 — a bad path must not sink delivery
            log.warning("could not sign a link for %s: %s", path, exc)


def copy_to_folder(clips: list[dict], folder: str) -> dict:
    """Copy each clip into a (synced) folder under a readable name."""
    if not folder:
        return {
            "ok": False,
            "detail": "no delivery folder is configured (REELFORGE_DELIVERY_DIR)",
        }
    target = Path(folder)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "detail": f"delivery folder unusable: {exc}"}

    copied: list[str] = []
    for clip in clips:
        src = Path(clip.get("exportPath", ""))
        if not src.is_file():
            continue
        name = safe_filename(str(clip.get("title") or ""), f"{clip.get('clipId', 'clip')}.mp4")
        dest = target / name
        # Two clips can share a title; never silently overwrite one. The
        # suffix is added, never edited into the name — a clip called
        # "take 2" must not be filed as "take 3".
        stem, n = dest.stem, 2
        while dest.exists():
            dest = target / f"{stem} ({n}).mp4"
            n += 1
        try:
            shutil.copy2(src, dest)
            copied.append(dest.name)
            clip["savedAs"] = str(dest)
        except OSError as exc:
            log.warning("could not copy %s: %s", src, exc)
    return {"ok": bool(copied), "count": len(copied), "files": copied, "folder": str(target)}


def _smtp_config() -> dict[str, str]:
    return {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": os.environ.get("SMTP_PORT", "587"),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", ""),
        "to": os.environ.get("SMTP_TO", ""),
    }


def build_email(clips: list[dict], project_name: str) -> EmailMessage:
    msg = EmailMessage()
    count = len(clips)
    msg["Subject"] = f"{count} clip{'' if count == 1 else 's'} ready — {project_name}"
    lines = [f"{count} clip{'' if count == 1 else 's'} from {project_name}:", ""]
    for clip in clips:
        title = clip.get("title") or clip.get("clipId", "clip")
        seconds = clip.get("durationSec")
        lines.append(f"- {title}" + (f" ({seconds:.0f}s)" if seconds else ""))
        if clip.get("url"):
            lines.append(f"  {clip['url']}")
        if clip.get("savedAs"):
            lines.append(f"  saved to {clip['savedAs']}")
        lines.append("")
    hours = round(links.MEDIA_LINK_TTL_S / 3600)
    lines.append(f"Links stop working after {hours} hours.")
    msg.set_content("\n".join(lines))
    return msg


def send_email(clips: list[dict], project_name: str) -> dict:
    cfg = _smtp_config()
    missing = [k for k in ("host", "sender", "to") if not cfg[k]]
    if missing:
        return {
            "ok": False,
            "detail": "email isn't set up (needs SMTP_HOST, SMTP_FROM, SMTP_TO)",
        }
    msg = build_email(clips, project_name)
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["to"]
    try:
        port = int(cfg["port"] or 587)
        if port == 465:
            server = smtplib.SMTP_SSL(cfg["host"], port, timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], port, timeout=30)
        with server:
            if port != 465:
                try:
                    server.starttls()
                except smtplib.SMTPException:
                    # A local relay may not offer TLS; the message still goes.
                    log.info("SMTP server did not accept STARTTLS; continuing")
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001 — never fail a cut over email
        log.warning("delivery email failed: %s", exc)
        return {"ok": False, "detail": f"could not send: {str(exc)[:200]}"}
    return {"ok": True, "to": cfg["to"]}


def deliver(clips: list[dict], channels: list[str], project_name: str) -> dict:
    """Run the chosen channels. Always mints links first — folder copies and
    the email both reference them."""
    report: dict = {"channels": channels}
    if not clips:
        return report
    add_links(clips)
    report["links"] = {"ok": True, "count": sum(1 for c in clips if c.get("url"))}
    if "folder" in channels:
        report["folder"] = copy_to_folder(clips, os.environ.get("REELFORGE_DELIVERY_DIR", ""))
    if "email" in channels:
        report["email"] = send_email(clips, project_name)
    return report
