"""Instagram + TikTok publish cores: auth URLs, chunk planning, mocked flows."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from reelforge_core.publish import instagram, tiktok
from reelforge_core.publish.youtube import PublishError

_RealClient = httpx.Client


def _client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        return _RealClient(transport=transport)

    return factory


# ---- auth URLs -------------------------------------------------------------


def test_instagram_auth_url_shape():
    url = instagram.auth_url("app123", "http://localhost:8001/cb", "st8")
    assert url.startswith("https://www.instagram.com/oauth/authorize?")
    assert "client_id=app123" in url
    assert "instagram_business_content_publish" in url
    assert "state=st8" in url


def test_tiktok_auth_url_shape():
    url = tiktok.auth_url("key123", "http://localhost:8001/cb", "st9")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert "client_key=key123" in url
    assert "video.upload" in url
    assert "state=st9" in url


# ---- TikTok chunk planning -------------------------------------------------


def test_tiktok_chunks_small_file_single_chunk():
    size = 3 * 1024 * 1024
    assert tiktok._plan_chunks(size) == (size, 1)


def test_tiktok_chunks_medium_file_single_chunk():
    size = 40 * 1024 * 1024
    assert tiktok._plan_chunks(size) == (size, 1)


def test_tiktok_chunks_large_file():
    size = 150 * 1024 * 1024
    chunk, count = tiktok._plan_chunks(size)
    assert chunk == tiktok.DEFAULT_CHUNK
    assert count == size // tiktok.DEFAULT_CHUNK
    # Last chunk absorbs the remainder; all bytes covered.
    assert (count - 1) * chunk + (size - (count - 1) * chunk) == size


# ---- Instagram publish flow (mocked) ---------------------------------------


def test_instagram_publish_reel_flow(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(instagram, "CONTAINER_POLL_INTERVAL_S", 0.0)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path.endswith("/ig-user-9/media"):
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert body["media_type"] == "REELS"
            assert "video_url" in body
            return httpx.Response(200, json={"id": "container-1"})
        if path.endswith("/container-1"):
            # First poll in progress, then finished.
            n = sum(1 for c in calls if c.endswith("/container-1"))
            status = "IN_PROGRESS" if n == 1 else "FINISHED"
            return httpx.Response(200, json={"status_code": status})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "media-77"})
        if path.endswith("/media-77"):
            return httpx.Response(
                200, json={"permalink": "https://www.instagram.com/reel/abc/"}
            )
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    media_id, permalink = instagram.publish_reel(
        "tok", "ig-user-9", "https://x.example/v.mp4", "caption"
    )
    assert media_id == "media-77"
    assert permalink == "https://www.instagram.com/reel/abc/"


def test_instagram_container_error_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(instagram, "CONTAINER_POLL_INTERVAL_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "c2"})
        return httpx.Response(200, json={"status_code": "ERROR"})

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    with pytest.raises(PublishError, match="processing failed"):
        instagram.publish_reel("tok", "u", "https://x/v.mp4", "c")


# ---- TikTok inbox upload flow (mocked) -------------------------------------


def test_tiktok_inbox_upload_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tiktok, "STATUS_POLL_INTERVAL_S", 0.0)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"z" * 4096)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/inbox/video/init/"):
            body = json.loads(request.content)
            seen["init"] = body["source_info"]
            return httpx.Response(
                200,
                json={
                    "data": {"publish_id": "pub-1", "upload_url": "https://up.tiktok/x"},
                    "error": {"code": "ok"},
                },
            )
        if request.method == "PUT":
            seen["range"] = request.headers["content-range"]
            return httpx.Response(200)
        if path.endswith("/status/fetch/"):
            return httpx.Response(
                200, json={"data": {"status": "SEND_TO_USER_INBOX"}}
            )
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    publish_id = tiktok.upload_to_inbox("tok", video)
    assert publish_id == "pub-1"
    assert seen["init"] == {
        "source": "FILE_UPLOAD",
        "video_size": 4096,
        "chunk_size": 4096,
        "total_chunk_count": 1,
    }
    assert seen["range"] == "bytes 0-4095/4096"


def test_tiktok_pending_limit_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"z" * 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {},
                "error": {"code": "spam_risk_too_many_pending_share", "message": "x"},
            },
        )

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    with pytest.raises(PublishError, match="max 5 pending"):
        tiktok.upload_to_inbox("tok", video)
