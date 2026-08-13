"""Social publishing endpoints: connect preconditions, publish contract."""

from __future__ import annotations

import pytest


async def _project_asset_reel(api_client) -> tuple[str, str, str]:
    r = await api_client.post("/api/v1/projects", json={"name": "social-test"})
    pid = r.json()["id"]
    from apps.api import db as dbmod

    aid = "e" * 64
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=120,
                width=1920,
                height=1080,
                fps=30,
                has_audio=True,
                size_bytes=1,
                probe_json="{}",
            )
        )
        session.add(
            dbmod.Reel(
                id="reel-social-1",
                project_id=pid,
                asset_id=aid,
                rank=1,
                title="publish me",
                hook="h",
                justification="j",
                start_sec=0.0,
                end_sec=60.0,
                duration_sec=60.0,
                overall_score=80,
                suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":80,"hook_strength":80,"emotional_payoff":80,"standalone_clarity":80}',
            )
        )
        await session.commit()
    return pid, aid, "reel-social-1"


async def _connect_fake_account() -> None:
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.SocialAccount(
                platform="youtube",
                access_token="at",
                refresh_token="rt",
                display_name="Test Channel",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_accounts_empty_initially(api_client) -> None:
    r = await api_client.get("/api/v1/social/accounts")
    assert r.status_code == 200
    assert r.json() == {"accounts": []}


@pytest.mark.asyncio
async def test_connect_without_google_config_409(api_client) -> None:
    r = await api_client.get("/api/v1/social/youtube/connect", follow_redirects=False)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SOCIAL_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_connect_redirects_to_google_when_configured(
    api_client, monkeypatch
) -> None:
    import apps.api.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "google_client_id", "cid")
    monkeypatch.setattr(settings_mod.settings, "google_client_secret", "sec")
    r = await api_client.get("/api/v1/social/youtube/connect", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "youtube.upload" in loc
    assert "access_type=offline" in loc


@pytest.mark.asyncio
async def test_publish_requires_connected_account(api_client) -> None:
    _, _, reel_id = await _project_asset_reel(api_client)
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SOCIAL_NOT_CONNECTED"


@pytest.mark.asyncio
async def test_publish_requires_export_on_disk(api_client) -> None:
    _, _, reel_id = await _project_asset_reel(api_client)
    await _connect_fake_account()
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EXPORT_NOT_READY"


@pytest.mark.asyncio
async def test_publish_rejects_prores_preset(api_client) -> None:
    _, _, reel_id = await _project_asset_reel(api_client)
    await _connect_fake_account()
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "preset_id": "mov_prores_hq"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_publish_happy_path_enqueues_job(api_client) -> None:
    import apps.api.settings as settings_mod

    _, aid, reel_id = await _project_asset_reel(api_client)
    await _connect_fake_account()
    out = settings_mod.settings.data_dir / "outputs" / aid / reel_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "mp4_h264_social.mp4").write_bytes(b"x" * 1024)

    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "description": "d", "privacy": "unlisted"},
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "publish"
    assert job["status"] == "queued"

    pubs = await api_client.get(f"/api/v1/reels/{reel_id}/publications")
    body = pubs.json()["publications"]
    assert len(body) == 1
    assert body[0]["status"] == "queued"
    assert body[0]["privacy"] == "unlisted"
    assert body[0]["publish_job_id"] == job["id"]


@pytest.mark.asyncio
async def test_disconnect_removes_account(api_client) -> None:
    await _connect_fake_account()
    r = await api_client.delete("/api/v1/social/youtube")
    assert r.status_code == 204
    r2 = await api_client.get("/api/v1/social/accounts")
    assert r2.json() == {"accounts": []}
