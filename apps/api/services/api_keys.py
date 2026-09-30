"""Mint / hash / verify the bearer keys agents use (see db.ApiKey).

ReelForge has no accounts: a key is the whole credential, so the rules that
matter are that the token is stored only as a hash, compared in constant
time, and revocable. Emailblaster and growth-agent verify through a
SECURITY DEFINER Postgres function to escape RLS; SQLite has neither, so the
lookup is a plain indexed query here.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api import db as dbmod

# Identifies our keys in a log or a paste, and lets `parse_bearer` reject
# anything that plainly isn't one before touching the database.
KEY_PREFIX = "rf_"
PREFIX_LENGTH = 11  # "rf_" + 8 token characters

_BEARER = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


def mint() -> tuple[str, str, str]:
    """`(token, prefix, token_hash)`. The token is shown once, never stored."""
    token = KEY_PREFIX + secrets.token_urlsafe(32)
    return token, token[:PREFIX_LENGTH], hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_bearer(header: str | None) -> str | None:
    """The key from an Authorization header (`Bearer rf_…`, or bare)."""
    if not header:
        return None
    m = _BEARER.match(header.strip())
    token = m.group(1) if m else header.strip()
    return token if token.startswith(KEY_PREFIX) and len(token) > PREFIX_LENGTH else None


async def verify(db: AsyncSession, token: str) -> dbmod.ApiKey | None:
    """The live key row for this token, else None. Stamps `last_used_at`."""
    row = (
        await db.execute(
            select(dbmod.ApiKey).where(
                dbmod.ApiKey.prefix == token[:PREFIX_LENGTH],
                dbmod.ApiKey.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    # Constant-time compare: the prefix already narrowed this to one row, so a
    # timing difference here would leak how much of a guessed key was right.
    if row is None or not secrets.compare_digest(row.token_hash, hash_token(token)):
        return None
    row.last_used_at = datetime.now(timezone.utc)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
