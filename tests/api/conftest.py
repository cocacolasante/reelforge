"""API tests need a live DB + an arq pool + a redis client. To avoid standing
up the full compose stack, we:

- use a temporary SQLite database bound to `REELFORGE_DATA_DIR`
- use `fakeredis` as a stand-in for Redis (arq accepts any redis-protocol client
  — we only exercise hset/hgetall/enqueue plumbing, not arq worker execution)

The arq pool is itself backed by fakeredis; jobs are enqueued (so we can see
them in the queue) but never executed, which is exactly what these API tests
want — they verify enqueue contracts, not end-to-end job runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def api_client(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    # Patch Settings via env vars so the db path lands in isolated_data_dir.
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(isolated_data_dir))

    # Rebuild settings + app per test.
    from apps.api import db as dbmod
    import apps.api.settings as settings_mod
    from apps.api.main import create_app
    import apps.api.main as main_mod

    # Mutate the module-level Settings singleton so every module that did
    # `from apps.api.settings import settings` sees the new data_dir.
    settings_mod.settings.data_dir = isolated_data_dir

    # Make the lifespan use fake redis for both arq pool + plain client.
    fake = fakeredis.FakeRedis(decode_responses=True)

    async def _fake_create_pool(*a, **kw):
        # arq's pool expects raw (bytes) redis. Provide a no-decode fake.
        pool = fakeredis.FakeRedis(decode_responses=False)

        async def enqueue_job(function_name, *args, _job_id=None, **kwargs):
            class _JobHandle:
                pass

            jh = _JobHandle()
            jh.job_id = _job_id
            return jh

        pool.enqueue_job = enqueue_job  # type: ignore[attr-defined]
        return pool  # fakeredis already provides aclose()

    monkeypatch.setattr(main_mod, "create_pool", _fake_create_pool)
    monkeypatch.setattr(main_mod.redis_async, "from_url", lambda *a, **kw: fake)

    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Manually trigger startup + shutdown via the lifespan context.
        async with app.router.lifespan_context(app):
            yield client
