"""TikTok publishing — Content Posting API, inbox (draft) flow.

Unaudited TikTok apps cannot direct-post publicly, so we use the inbox flow:
the video uploads straight from this machine (FILE_UPLOAD — no public URL
needed), lands in the user's TikTok inbox, and they finish captioning/posting
inside the TikTok app. This works without app audit and keeps the human in
control of the final post.

Token model: access token lasts 24h; refresh token lasts 365 days and MAY
ROTATE on refresh — always store the returned refresh_token.

All functions sync; callers run them in a thread. Failures raise PublishError.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from reelforge_core.publish.youtube import PublishError

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_ENDPOINT = "https://open.tiktokapis.com/v2/user/info/"
INBOX_INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.upload"

# FILE_UPLOAD chunk rules: 5 MiB <= chunk <= 64 MiB; files smaller than the
# minimum upload as one whole-file chunk; the final chunk absorbs remainders.
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024

STATUS_POLL_INTERVAL_S = 5.0
STATUS_POLL_TIMEOUT_S = 300.0


def auth_url(client_key: str, redirect_uri: str, state: str) -> str:
    return AUTH_ENDPOINT + "?" + urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def _token_call(data: dict) -> dict:
    import httpx

    resp = httpx.post(
        TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or resp.text[:300]
        raise PublishError(f"TikTok token call failed ({resp.status_code}): {err}")
    return body


def exchange_code(
    client_key: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Returns {access_token, refresh_token, expires_in, open_id, ...}."""
    return _token_call(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )


def refresh_tokens(client_key: str, client_secret: str, refresh_token: str) -> dict:
    """Returns fresh {access_token, refresh_token, expires_in, ...}. The
    refresh token may rotate — persist the returned one."""
    return _token_call(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )


def fetch_profile(access_token: str) -> dict:
    """Returns {"open_id", "display_name"}."""
    import httpx

    resp = httpx.get(
        USER_INFO_ENDPOINT,
        params={"fields": "open_id,display_name"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    data = resp.json().get("data", {}).get("user", {}) if resp.status_code == 200 else {}
    if not data.get("open_id"):
        raise PublishError(
            f"TikTok profile lookup failed ({resp.status_code}): {resp.text[:300]}"
        )
    return {"open_id": data["open_id"], "display_name": data.get("display_name")}


def _plan_chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's FILE_UPLOAD rules."""
    if size <= MAX_CHUNK:
        return size, 1
    count = size // DEFAULT_CHUNK
    return DEFAULT_CHUNK, count


def upload_to_inbox(
    access_token: str,
    video_path: Path,
    progress_cb: Callable[[float], None] | None = None,
) -> str:
    """Upload a video to the user's TikTok inbox. Returns the publish_id.

    Terminal success for the inbox flow is SEND_TO_USER_INBOX — the user
    finishes captioning + posting in the TikTok app.
    """
    import httpx

    size = video_path.stat().st_size
    if size <= 0:
        raise PublishError(f"{video_path} is empty")
    chunk_size, total_chunks = _plan_chunks(size)

    with httpx.Client(timeout=120) as client:
        init = client.post(
            INBOX_INIT_ENDPOINT,
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                }
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        body = init.json() if init.content else {}
        err = (body.get("error") or {}).get("code", "ok")
        if init.status_code != 200 or err != "ok":
            msg = (body.get("error") or {}).get("message") or init.text[:300]
            if "spam_risk_too_many_pending_share" in err:
                msg = (
                    "TikTok inbox limit reached (max 5 pending uploads per 24h) — "
                    "open TikTok and post or discard pending drafts first."
                )
            raise PublishError(f"TikTok upload init failed: {msg}")
        publish_id = body["data"]["publish_id"]
        upload_url = body["data"]["upload_url"]

        sent = 0
        chunk_index = 0
        with open(video_path, "rb") as f:
            while sent < size:
                chunk_index += 1
                is_last = chunk_index == total_chunks
                # The final chunk absorbs the remainder.
                this_size = (size - sent) if is_last else chunk_size
                chunk = f.read(this_size)
                if not chunk:
                    break
                end = sent + len(chunk) - 1
                put = client.put(
                    upload_url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{size}",
                    },
                    content=chunk,
                )
                if put.status_code not in (200, 201, 206):
                    raise PublishError(
                        f"TikTok chunk upload failed ({put.status_code}): {put.text[:300]}"
                    )
                sent = end + 1
                if progress_cb:
                    progress_cb(0.1 + 0.7 * (sent / size))

        # Poll until TikTok has ingested the upload into the user's inbox.
        deadline = time.monotonic() + STATUS_POLL_TIMEOUT_S
        while True:
            status_resp = client.post(
                STATUS_ENDPOINT,
                json={"publish_id": publish_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = (
                status_resp.json().get("data", {})
                if status_resp.status_code == 200
                else {}
            )
            status = data.get("status")
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                if progress_cb:
                    progress_cb(1.0)
                return publish_id
            if status == "FAILED":
                reason = data.get("fail_reason") or "unknown"
                raise PublishError(f"TikTok processing failed: {reason}")
            if time.monotonic() > deadline:
                raise PublishError("TikTok upload did not finish processing in time")
            if progress_cb:
                progress_cb(0.9)
            time.sleep(STATUS_POLL_INTERVAL_S)
