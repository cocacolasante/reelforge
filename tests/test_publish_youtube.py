"""YouTube publish core: auth URL shape + resumable upload protocol (mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from reelforge_core.publish import youtube


def test_auth_url_shape():
    url = youtube.auth_url("cid", "http://localhost:8001/cb", "state123")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=state123" in url


_RealClient = httpx.Client


def _client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        return _RealClient(transport=transport)

    return factory


def test_upload_video_single_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["metadata"] = json.loads(request.content)
            assert request.headers["x-upload-content-length"] == "1000"
            return httpx.Response(
                200, headers={"location": "https://upload.example/session"}
            )
        assert request.method == "PUT"
        seen["range"] = request.headers["content-range"]
        return httpx.Response(200, json={"id": "vid123"})

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    vid = youtube.upload_video(
        "tok", video, title="T", description="D", privacy="unlisted"
    )
    assert vid == "vid123"
    assert seen["range"] == "bytes 0-999/1000"
    assert seen["metadata"]["status"]["privacyStatus"] == "unlisted"
    assert seen["metadata"]["snippet"]["title"] == "T"


def test_upload_video_multi_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(youtube, "UPLOAD_CHUNK", 600)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, headers={"location": "https://upload.example/session"}
            )
        ranges.append(request.headers["content-range"])
        if len(ranges) == 1:
            return httpx.Response(308)
        return httpx.Response(201, json={"id": "vid456"})

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    progress: list[float] = []
    vid = youtube.upload_video(
        "tok", video, title="T", description="", progress_cb=progress.append
    )
    assert vid == "vid456"
    assert ranges == ["bytes 0-599/1000", "bytes 600-999/1000"]
    assert progress[-1] == 1.0


def test_upload_session_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="quota exceeded")

    monkeypatch.setattr(httpx, "Client", _client_factory(handler))
    with pytest.raises(youtube.PublishError, match="quota"):
        youtube.upload_video("tok", video, title="T", description="")
