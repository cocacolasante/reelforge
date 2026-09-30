"""HMAC-signed, expiring links, and the media links they carry.

Lives in core because both sides need it: the API mints and serves links,
the worker puts them in a delivery email.

The link IS the capability: anyone holding an unexpired one can use it, so
they are short-lived and carry only what the route needs (which project to
upload into, which file to serve). Used by the phone upload page and, later,
the media links an agent hands back.

The secret lives in a file under the data dir rather than the environment:
it must survive a restart — otherwise every outstanding link dies — and
generating it on first use means there is no setup step to forget.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from reelforge_core.paths import DATA_DIR

log = logging.getLogger(__name__)

# Long enough that a link still works when the notification is opened next
# morning; short enough that one forwarded on stops working.
MEDIA_LINK_TTL_S = 48 * 60 * 60

_SECRET_FILENAME = ".link_secret"
_cached_secret: bytes | None = None


def _data_dir() -> Path:
    """Read at call time, not import: tests relocate the data dir per case."""
    return Path(os.environ.get("REELFORGE_DATA_DIR", str(DATA_DIR)))


def _secret() -> bytes:
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    path = _data_dir() / _SECRET_FILENAME
    if path.exists():
        _cached_secret = path.read_bytes().strip()
        return _cached_secret
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48).encode()
    # Write then chmod through a temp file so the secret is never briefly
    # world-readable, and so two workers racing can't read a half-written one.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(value)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    log.info("generated a link-signing secret at %s", path)
    _cached_secret = value
    return value


def reset_cache() -> None:
    """Forget the in-process secret (tests move the data dir between cases)."""
    global _cached_secret
    _cached_secret = None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict[str, Any], ttl_seconds: int) -> str:
    """A token carrying `payload`, valid for `ttl_seconds`."""
    body = dict(payload)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(_secret(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(mac)}"


def verify(token: str) -> dict[str, Any] | None:
    """The payload if the signature holds and it hasn't expired, else None."""
    try:
        body_b64, mac_b64 = token.split(".", 1)
        raw = _unb64(body_b64)
        expected = hmac.new(_secret(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(mac_b64)):
            return None
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 — any malformed token is simply invalid
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < time.time():
        return None
    return payload


# --- media links ------------------------------------------------------------

MEDIA_PURPOSE = "media"


def _outputs_dir() -> Path:
    return _data_dir() / "outputs"


def media_token(export_path: Path | str, ttl_seconds: int = MEDIA_LINK_TTL_S) -> str:
    """A token naming one exported file. Only the path RELATIVE to the
    outputs directory is signed, so a token cannot name anything outside it."""
    rel = Path(export_path).resolve().relative_to(_outputs_dir().resolve())
    return sign({"p": MEDIA_PURPOSE, "f": str(rel)}, ttl_seconds)


def resolve_media_token(token: str) -> Path | None:
    """The file a token names, or None. Containment is re-checked here: the
    signature proves we minted it, not that it still points somewhere sane."""
    payload = verify(token)
    if payload is None or payload.get("p") != MEDIA_PURPOSE:
        return None
    outputs = _outputs_dir().resolve()
    try:
        path = (outputs / str(payload.get("f", ""))).resolve()
        path.relative_to(outputs)
    except (ValueError, OSError):
        return None
    return path if path.is_file() else None


def media_url(
    export_path: Path | str, public_base: str, ttl_seconds: int = MEDIA_LINK_TTL_S
) -> str:
    return f"{public_base.rstrip('/')}/media/{media_token(export_path, ttl_seconds)}"
