"""Phase 0 /health shape smoke test. Uses the api_client fixture from
tests/api/conftest.py so the FastAPI lifespan (db engine, redis client, arq
pool) is set up before the endpoint is hit.

The newer Phase-5 test_projects::test_health_endpoint covers the same ground
more directly; this file is kept for historical coverage.
"""

from __future__ import annotations

# Pull the api_client fixture in.
from tests.api.conftest import api_client  # noqa: F401


async def test_health_shape(api_client) -> None:
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "status",
        "ffmpeg",
        "redis",
        "whisper_models_cache_gb",
        "anthropic_configured",
        "anthropic_reachable",
        "data_dir",
    ):
        assert key in body, f"missing {key} in /health payload"
    assert body["status"] in {"ok", "degraded"}
