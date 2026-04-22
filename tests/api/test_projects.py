from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_and_list_projects(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "Proj A"})
    assert r.status_code == 201, r.text
    pa = r.json()
    assert pa["name"] == "Proj A"
    assert "id" in pa

    r = await api_client.post("/api/v1/projects", json={"name": "Proj B"})
    pb = r.json()

    r = await api_client.get("/api/v1/projects")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    ids = {p["id"] for p in body["projects"]}
    assert pa["id"] in ids and pb["id"] in ids


@pytest.mark.asyncio
async def test_project_not_found_returns_envelope(api_client) -> None:
    r = await api_client.get("/api/v1/projects/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "PROJECT_NOT_FOUND"
    assert "request_id" in body


@pytest.mark.asyncio
async def test_delete_project(api_client) -> None:
    r = await api_client.post("/api/v1/projects", json={"name": "to-delete"})
    pid = r.json()["id"]
    r = await api_client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 204
    r = await api_client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_health_endpoint(api_client) -> None:
    r = await api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    for key in ("status", "ffmpeg", "redis", "anthropic_configured"):
        assert key in body


@pytest.mark.asyncio
async def test_ready_endpoint(api_client) -> None:
    r = await api_client.get("/ready")
    assert r.status_code == 200
    assert r.json()["ready"] is True


@pytest.mark.asyncio
async def test_request_id_header_returned(api_client) -> None:
    r = await api_client.get("/health")
    assert "x-request-id" in {k.lower() for k in r.headers.keys()}
