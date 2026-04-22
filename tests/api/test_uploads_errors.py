"""Upload error paths that the happy-path test doesn't hit."""

from __future__ import annotations

import pytest

CHUNK = 64 * 1024  # matches the server-side minimum


@pytest.mark.asyncio
async def test_chunk_on_nonexistent_session(api_client) -> None:
    r = await api_client.put(
        "/api/v1/uploads/does-not-exist/chunks/0",
        content=b"x" * CHUNK,
        headers={"content-length": str(CHUNK)},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_complete_on_nonexistent_session(api_client) -> None:
    r = await api_client.post("/api/v1/uploads/does-not-exist/complete")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_abort_nonexistent_session(api_client) -> None:
    r = await api_client.delete("/api/v1/uploads/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_abort_session(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "abort"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 2 * CHUNK,
            "chunk_size": CHUNK,
        },
    )
    upload_id = r.json()["id"]
    r = await api_client.delete(f"/api/v1/uploads/{upload_id}")
    assert r.status_code == 204
    # Session should now be aborted; further chunk PUTs are rejected.
    r = await api_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/0",
        content=b"x" * CHUNK,
        headers={"content-length": str(CHUNK)},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "UPLOAD_ALREADY_COMPLETED"


@pytest.mark.asyncio
async def test_chunk_index_out_of_range(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "oob"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 2 * CHUNK,
            "chunk_size": CHUNK,
        },
    )
    upload_id = r.json()["id"]
    # Only 2 chunks expected; index 5 is out of range.
    r = await api_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/5",
        content=b"x" * CHUNK,
        headers={"content-length": str(CHUNK)},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UPLOAD_CHUNK_OUT_OF_ORDER"


@pytest.mark.asyncio
async def test_chunk_size_mismatch(api_client) -> None:
    """Server expects CHUNK bytes for non-last chunk; sending 256 should reject."""
    r = await api_client.post("/api/v1/projects", json={"name": "mismatch"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 2 * CHUNK,
            "chunk_size": CHUNK,
        },
    )
    upload_id = r.json()["id"]
    r = await api_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/0",
        content=b"x" * 256,
        headers={"content-length": "256"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_complete_before_all_chunks(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "incomplete"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 2 * CHUNK,
            "chunk_size": CHUNK,
        },
    )
    upload_id = r.json()["id"]
    # Send only chunk 0; then call complete.
    await api_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/0",
        content=b"x" * CHUNK,
        headers={"content-length": str(CHUNK)},
    )
    r = await api_client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UPLOAD_CHUNK_OUT_OF_ORDER"


@pytest.mark.asyncio
async def test_upload_project_not_found(api_client) -> None:
    r = await api_client.post(
        "/api/v1/projects/does-not-exist/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 2 * CHUNK,
            "chunk_size": CHUNK,
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.asyncio
async def test_upload_chunk_size_below_floor(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "small-chunk"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 1024,
            "chunk_size": 1024,  # below 64 KiB floor
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CONFIG"


@pytest.mark.asyncio
async def test_upload_session_status_reflects_received_bytes(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "status"})
    pid = r.json()["id"]
    r = await api_client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "filename": "x.mp4",
            "content_type": "video/mp4",
            "total_bytes": 2 * CHUNK,
            "chunk_size": CHUNK,
        },
    )
    upload_id = r.json()["id"]
    await api_client.put(
        f"/api/v1/uploads/{upload_id}/chunks/0",
        content=b"x" * CHUNK,
        headers={"content-length": str(CHUNK)},
    )
    r = await api_client.get(f"/api/v1/uploads/{upload_id}")
    assert r.json()["received_bytes"] == CHUNK
    assert r.json()["status"] == "active"
