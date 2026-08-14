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


async def _connect_fake_account(
    external_id: str = "chan-1", name: str = "Test Channel"
) -> str:
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        acct = dbmod.SocialAccount(
            platform="youtube",
            external_id=external_id,
            access_token="at",
            refresh_token="rt",
            display_name=name,
        )
        session.add(acct)
        await session.commit()
        return acct.id


@pytest.mark.asyncio
async def test_accounts_empty_initially(api_client) -> None:
    r = await api_client.get("/api/v1/social/accounts")
    assert r.status_code == 200
    assert r.json() == {"accounts": []}


@pytest.mark.asyncio
async def test_connect_without_google_config_409(api_client, monkeypatch) -> None:
    # The test env may carry real credentials via .env — force-unset so this
    # exercises the unconfigured path deterministically.
    import apps.api.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "google_client_id", "")
    monkeypatch.setattr(settings_mod.settings, "google_client_secret", "")
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
    acct_id = await _connect_fake_account()
    r = await api_client.delete(f"/api/v1/social/accounts/{acct_id}")
    assert r.status_code == 204
    r2 = await api_client.get("/api/v1/social/accounts")
    assert r2.json() == {"accounts": []}


@pytest.mark.asyncio
async def test_publish_multiple_channels_requires_account_id(api_client) -> None:
    import apps.api.settings as settings_mod

    _, aid, reel_id = await _project_asset_reel(api_client)
    id_a = await _connect_fake_account("chan-a", "Channel A")
    id_b = await _connect_fake_account("chan-b", "Channel B")
    out = settings_mod.settings.data_dir / "outputs" / aid / reel_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "mp4_h264_social.mp4").write_bytes(b"x" * 1024)

    # Ambiguous: two channels, no account_id.
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish", json={"title": "hello"}
    )
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "CHANNEL_REQUIRED"
    names = {c["display_name"] for c in body["details"]["channels"]}
    assert names == {"Channel A", "Channel B"}

    # Explicit channel works and is snapshotted on the publication.
    r2 = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "account_id": id_b},
    )
    assert r2.status_code == 200, r2.text
    pubs = (await api_client.get(f"/api/v1/reels/{reel_id}/publications")).json()
    assert pubs["publications"][0]["channel_title"] == "Channel B"

    # Unknown channel id -> 404.
    r3 = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "account_id": "nope"},
    )
    assert r3.status_code == 404
    assert id_a  # silence unused warning


# ---- Instagram / TikTok ----------------------------------------------------


@pytest.mark.asyncio
async def test_instagram_connect_unconfigured_409(api_client, monkeypatch) -> None:
    import apps.api.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "instagram_app_id", "")
    r = await api_client.get("/api/v1/social/instagram/connect", follow_redirects=False)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SOCIAL_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_instagram_connect_redirects_when_configured(api_client, monkeypatch) -> None:
    import apps.api.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "instagram_app_id", "ig-app")
    monkeypatch.setattr(settings_mod.settings, "instagram_app_secret", "ig-sec")
    r = await api_client.get("/api/v1/social/instagram/connect", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://www.instagram.com/oauth/authorize?")
    assert "instagram_business_content_publish" in loc


@pytest.mark.asyncio
async def test_tiktok_connect_redirects_when_configured(api_client, monkeypatch) -> None:
    import apps.api.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "tiktok_client_key", "tt-key")
    monkeypatch.setattr(settings_mod.settings, "tiktok_client_secret", "tt-sec")
    r = await api_client.get("/api/v1/social/tiktok/connect", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert "video.upload" in loc


async def _connect_platform_account(platform: str, external_id: str, name: str) -> str:
    from apps.api import db as dbmod

    async with dbmod.db_state.sessionmaker() as session:
        acct = dbmod.SocialAccount(
            platform=platform,
            external_id=external_id,
            access_token="at",
            refresh_token="rt",
            display_name=name,
        )
        session.add(acct)
        await session.commit()
        return acct.id


@pytest.mark.asyncio
async def test_instagram_publish_requires_public_base(api_client, monkeypatch) -> None:
    import apps.api.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "public_media_base", "")
    _, aid, reel_id = await _project_asset_reel(api_client)
    await _connect_platform_account("instagram", "ig-1", "@snowilder")
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "platform": "instagram"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PUBLIC_URL_REQUIRED"


@pytest.mark.asyncio
async def test_instagram_publish_mints_public_token_and_serves_media(
    api_client, monkeypatch
) -> None:
    import apps.api.settings as settings_mod

    monkeypatch.setattr(
        settings_mod.settings, "public_media_base", "https://x.trycloudflare.com"
    )
    _, aid, reel_id = await _project_asset_reel(api_client)
    await _connect_platform_account("instagram", "ig-2", "@snowrider")
    out = settings_mod.settings.data_dir / "outputs" / aid / reel_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "mp4_h264_social.mp4").write_bytes(b"v" * 2048)

    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "platform": "instagram"},
    )
    assert r.status_code == 200, r.text

    # The public media route serves the export while the publication is live.
    from apps.api import db as dbmod
    from sqlalchemy import select as _select

    async with dbmod.db_state.sessionmaker() as session:
        pub = (
            await session.execute(
                _select(dbmod.Publication).where(dbmod.Publication.reel_id == reel_id)
            )
        ).scalars().first()
    assert pub.public_token
    media = await api_client.get(f"/api/v1/public/media/{pub.public_token}")
    assert media.status_code == 200
    assert media.content == b"v" * 2048

    # Wrong token -> 404.
    bad = await api_client.get("/api/v1/public/media/not-a-token")
    assert bad.status_code == 404


@pytest.mark.asyncio
async def test_tiktok_publish_enqueues(api_client) -> None:
    import apps.api.settings as settings_mod

    _, aid, reel_id = await _project_asset_reel(api_client)
    await _connect_platform_account("tiktok", "open-id-1", "snowtok")
    out = settings_mod.settings.data_dir / "outputs" / aid / reel_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "mp4_h264_social.mp4").write_bytes(b"v" * 1024)

    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "platform": "tiktok"},
    )
    assert r.status_code == 200, r.text
    pubs = (await api_client.get(f"/api/v1/reels/{reel_id}/publications")).json()
    assert pubs["publications"][0]["platform"] == "tiktok"
    assert pubs["publications"][0]["channel_title"] == "snowtok"


@pytest.mark.asyncio
async def test_platform_accounts_are_isolated(api_client) -> None:
    """A connected TikTok account must not satisfy a YouTube publish."""
    import apps.api.settings as settings_mod

    _, aid, reel_id = await _project_asset_reel(api_client)
    await _connect_platform_account("tiktok", "open-id-2", "snowtok2")
    out = settings_mod.settings.data_dir / "outputs" / aid / reel_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "mp4_h264_social.mp4").write_bytes(b"v" * 1024)
    r = await api_client.post(
        f"/api/v1/reels/{reel_id}/publish",
        json={"title": "hello", "platform": "youtube"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SOCIAL_NOT_CONNECTED"
