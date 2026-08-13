"""YouTube Data API v3 publishing — OAuth token flows + resumable upload.

Plain REST via httpx (no google-api-python-client dependency). All functions
are synchronous; callers run them in a thread. Errors raise PublishError with
a human-readable message — the worker records it on the publication row.

Requires a user-supplied Google Cloud OAuth client (docs/publishing.md).
Note: uploads from unverified OAuth apps are locked to private by YouTube
until the app passes verification — expected behavior, not a bug here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
UPLOAD_CHUNK = 8 * 1024 * 1024


class PublishError(Exception):
    pass


def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_ENDPOINT + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            # Force the consent screen so Google always returns a refresh
            # token — without this, re-connecting yields access-token-only.
            "prompt": "consent",
            "state": state,
        }
    )


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Returns {access_token, refresh_token, expires_in}."""
    import httpx

    resp = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(f"token exchange failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if "refresh_token" not in data:
        raise PublishError(
            "Google did not return a refresh token — disconnect the app at "
            "https://myaccount.google.com/permissions and connect again."
        )
    return data


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    import httpx

    resp = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(
            f"token refresh failed ({resp.status_code}): {resp.text[:300]} — "
            "reconnect the YouTube account."
        )
    return resp.json()["access_token"]


def fetch_channel_title(access_token: str) -> str:
    import httpx

    resp = httpx.get(
        CHANNELS_ENDPOINT,
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(f"channel lookup failed ({resp.status_code}): {resp.text[:300]}")
    items = resp.json().get("items", [])
    if not items:
        raise PublishError("no YouTube channel found on this Google account")
    return items[0]["snippet"]["title"]


def upload_video(
    access_token: str,
    video_path: Path,
    *,
    title: str,
    description: str,
    privacy: str = "private",
    tags: list[str] | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> str:
    """Resumable upload. Returns the YouTube video id."""
    import httpx

    size = video_path.stat().st_size
    if size <= 0:
        raise PublishError(f"{video_path} is empty")

    metadata = {
        "snippet": {
            "title": title[:100] or "ReelForge clip",
            "description": description[:4900],
            "tags": (tags or [])[:30],
            "categoryId": "22",  # People & Blogs — safe default
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    with httpx.Client(timeout=120) as client:
        start = client.post(
            UPLOAD_ENDPOINT,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            content=json.dumps(metadata),
        )
        if start.status_code != 200:
            raise PublishError(
                f"upload session failed ({start.status_code}): {start.text[:300]}"
            )
        location = start.headers.get("location")
        if not location:
            raise PublishError("upload session response had no Location header")

        sent = 0
        with open(video_path, "rb") as f:
            while sent < size:
                chunk = f.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                end = sent + len(chunk) - 1
                put = client.put(
                    location,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{size}",
                    },
                    content=chunk,
                )
                if put.status_code in (200, 201):
                    body = put.json()
                    video_id = body.get("id")
                    if not video_id:
                        raise PublishError(f"upload finished without a video id: {put.text[:300]}")
                    if progress_cb:
                        progress_cb(1.0)
                    return video_id
                if put.status_code != 308:
                    raise PublishError(
                        f"upload chunk failed ({put.status_code}): {put.text[:300]}"
                    )
                sent = end + 1
                if progress_cb:
                    progress_cb(sent / size)

    raise PublishError("upload ended without a completed response")


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
