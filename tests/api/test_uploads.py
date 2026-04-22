"""Chunked upload contract. Uses a small synthetic MP4 so the upload path
runs end-to-end including asset_id derivation and probe."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def tiny_upload_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("upload") / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1:r=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.mark.asyncio
async def test_upload_chunked_creates_asset(api_client, tiny_upload_mp4: Path) -> None:
    # 1) Create a project
    r = await api_client.post("/api/v1/projects", json={"name": "upload test"})
    project_id = r.json()["id"]

    # 2) Start upload session
    size = tiny_upload_mp4.stat().st_size
    chunk_size = 64 * 1024
    r = await api_client.post(
        f"/api/v1/projects/{project_id}/uploads",
        json={
            "filename": "tiny.mp4",
            "content_type": "video/mp4",
            "total_bytes": size,
            "chunk_size": chunk_size,
        },
    )
    assert r.status_code == 201, r.text
    upload_id = r.json()["id"]

    # 3) Push chunks
    with tiny_upload_mp4.open("rb") as f:
        idx = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            r = await api_client.put(
                f"/api/v1/uploads/{upload_id}/chunks/{idx}",
                content=chunk,
                headers={"content-length": str(len(chunk))},
            )
            assert r.status_code == 200, r.text
            idx += 1

    # 4) Session status shows all bytes received
    r = await api_client.get(f"/api/v1/uploads/{upload_id}")
    body = r.json()
    assert body["received_bytes"] == size
    assert body["status"] == "active"

    # 5) Complete
    r = await api_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert r.status_code == 200, r.text
    asset = r.json()
    assert asset["size_bytes"] == size
    assert asset["project_id"] == project_id
    assert len(asset["id"]) == 64  # sha256 hex

    # 6) Asset detail
    r = await api_client.get(f"/api/v1/assets/{asset['id']}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_upload_too_large_rejected(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "too-large"})
    pid = r.json()["id"]
    # Ask for 100 GB — well above the 5 GB default limit.
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "huge.mp4",
            "content_type": "video/mp4",
            "total_bytes": 100 * 1024**3,
            "chunk_size": 8 * 1024 * 1024,
        },
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_upload_non_video_rejected(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "wrong-mime"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "doc.pdf",
            "content_type": "application/pdf",
            "total_bytes": 1024,
            "chunk_size": 1024,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UPLOAD_UNSUPPORTED_TYPE"


@pytest.mark.asyncio
async def test_chunk_size_mismatch_rejected(api_client, tiny_upload_mp4: Path) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "mismatch"})
    pid = r.json()["id"]
    size = tiny_upload_mp4.stat().st_size
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "tiny.mp4",
            "content_type": "video/mp4",
            "total_bytes": size,
            "chunk_size": 1024 * 1024,  # 1 MB chunks but we'll send a different size
        },
    )
    upload_id = r.json()["id"]
    # Wrong content-length for chunk 0 (not the last chunk)
    r = await api_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/0",
        content=b"x" * 256,
        headers={"content-length": "256"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_upload_session_not_found(api_client) -> None:
    r = await api_client.get("/api/v1/uploads/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UPLOAD_SESSION_NOT_FOUND"
