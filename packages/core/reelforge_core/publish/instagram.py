"""Instagram Reels publishing — Instagram API with Instagram Login.

Business/Creator accounts only. The critical difference from YouTube/TikTok:
Meta's servers FETCH the video from a publicly reachable `video_url`, so the
caller must expose the export through a public HTTPS base (cloudflared
tunnel — see docs/publishing.md).

Token model: a single long-lived access token (~60 days), refreshed via
`refresh_access_token` once it's at least 24h old. No separate refresh token.

All functions sync; callers run them in a thread. Failures raise PublishError.
"""

from __future__ import annotations

import logging
import time
from typing import Callable
from urllib.parse import urlencode

from reelforge_core.publish.youtube import PublishError

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://www.instagram.com/oauth/authorize"
SHORT_TOKEN_ENDPOINT = "https://api.instagram.com/oauth/access_token"
GRAPH = "https://graph.instagram.com"
API_VERSION = "v25.0"
SCOPES = "instagram_business_basic,instagram_business_content_publish"

CONTAINER_POLL_INTERVAL_S = 10.0
CONTAINER_POLL_TIMEOUT_S = 480.0


def auth_url(app_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_ENDPOINT + "?" + urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )


def exchange_code(
    app_id: str, app_secret: str, code: str, redirect_uri: str
) -> dict:
    """Short-lived exchange, then upgrade to the 60-day long-lived token.

    Returns {"access_token", "expires_in", "user_id"}.
    """
    import httpx

    resp = httpx.post(
        SHORT_TOKEN_ENDPOINT,
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(
            f"Instagram token exchange failed ({resp.status_code}): {resp.text[:300]}"
        )
    short = resp.json()
    user_id = short.get("user_id")

    resp2 = httpx.get(
        f"{GRAPH}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": app_secret,
            "access_token": short["access_token"],
        },
        timeout=30,
    )
    if resp2.status_code != 200:
        raise PublishError(
            f"Instagram long-lived exchange failed ({resp2.status_code}): {resp2.text[:300]}"
        )
    long_lived = resp2.json()
    return {
        "access_token": long_lived["access_token"],
        "expires_in": long_lived.get("expires_in", 60 * 24 * 3600),
        "user_id": user_id,
    }


def refresh_token(access_token: str) -> dict:
    """Refresh a long-lived token (must be >=24h old). Returns
    {"access_token", "expires_in"}."""
    import httpx

    resp = httpx.get(
        f"{GRAPH}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": access_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(
            f"Instagram token refresh failed ({resp.status_code}): {resp.text[:300]} — "
            "reconnect the Instagram account."
        )
    return resp.json()


def fetch_profile(access_token: str) -> dict:
    """Returns {"user_id", "username"} for the authorized account."""
    import httpx

    resp = httpx.get(
        f"{GRAPH}/{API_VERSION}/me",
        params={"fields": "user_id,username", "access_token": access_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise PublishError(
            f"Instagram profile lookup failed ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    return {"user_id": str(data.get("user_id") or data.get("id")), "username": data.get("username")}


def publish_reel(
    access_token: str,
    ig_user_id: str,
    video_url: str,
    caption: str,
    progress_cb: Callable[[float], None] | None = None,
) -> tuple[str, str | None]:
    """Create a REELS container from `video_url`, wait for processing, publish.

    Returns (media_id, permalink). Total wall time is dominated by Meta
    fetching + transcoding the video (typically 30-120s).
    """
    import httpx

    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{GRAPH}/{API_VERSION}/{ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption[:2200],
                "access_token": access_token,
            },
        )
        if resp.status_code != 200:
            raise PublishError(
                f"Instagram container create failed ({resp.status_code}): {resp.text[:300]}"
            )
        container_id = resp.json().get("id")
        if not container_id:
            raise PublishError(f"no container id in response: {resp.text[:300]}")
        if progress_cb:
            progress_cb(0.15)

        # Wait for Meta to fetch + process the video.
        deadline = time.monotonic() + CONTAINER_POLL_TIMEOUT_S
        elapsed_frac = 0.15
        while True:
            status_resp = client.get(
                f"{GRAPH}/{API_VERSION}/{container_id}",
                params={"fields": "status_code", "access_token": access_token},
            )
            status = (
                status_resp.json().get("status_code")
                if status_resp.status_code == 200
                else None
            )
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Instagram processing failed (container status {status}) — "
                    "check the video is reachable at the public URL and is a "
                    "valid MP4 (9:16, <=90s for Reels)."
                )
            if time.monotonic() > deadline:
                raise PublishError(
                    "Instagram container did not finish processing in time"
                )
            elapsed_frac = min(0.85, elapsed_frac + 0.05)
            if progress_cb:
                progress_cb(elapsed_frac)
            time.sleep(CONTAINER_POLL_INTERVAL_S)

        resp = client.post(
            f"{GRAPH}/{API_VERSION}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": access_token},
        )
        if resp.status_code != 200:
            raise PublishError(
                f"Instagram publish failed ({resp.status_code}): {resp.text[:300]}"
            )
        media_id = resp.json().get("id")
        if not media_id:
            raise PublishError(f"no media id in publish response: {resp.text[:300]}")
        if progress_cb:
            progress_cb(0.95)

        permalink = None
        perma_resp = client.get(
            f"{GRAPH}/{API_VERSION}/{media_id}",
            params={"fields": "permalink", "access_token": access_token},
        )
        if perma_resp.status_code == 200:
            permalink = perma_resp.json().get("permalink")
        if progress_cb:
            progress_cb(1.0)
        return media_id, permalink
