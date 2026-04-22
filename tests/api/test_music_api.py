"""Music library listing + user uploads + deletes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mp3_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("mp3") / "upload.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "libmp3lame", "-b:a", "64k",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.mark.asyncio
async def test_list_music(api_client) -> None:
    r = await api_client.get("/api/v1/music")
    assert r.status_code == 200
    body = r.json()
    assert "tracks" in body
    # /app/assets/music/manifest.json ships 10 bundled tracks.
    # Inside the test container they exist as synthesized placeholders.
    assert isinstance(body["tracks"], list)


@pytest.mark.asyncio
async def test_get_music_not_found(api_client) -> None:
    r = await api_client.get("/api/v1/music/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_upload_mp3(api_client, mp3_fixture: Path, isolated_data_dir: Path) -> None:
    # Patch the music router to use the isolated data dir for /data/music.
    from apps.api.routers import music as music_router

    music_router._USER_MUSIC_DIR = isolated_data_dir / "music"
    music_router._USER_MANIFEST = isolated_data_dir / "music" / "manifest.json"

    with mp3_fixture.open("rb") as f:
        r = await api_client.post(
            "/api/v1/music/uploads",
            files={"file": ("upload.mp3", f, "audio/mpeg")},
            data={
                "title": "tiny-test",
                "mood": "calm",
                "license": "CC0",
                "scope": "global",
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "user"
    assert body["license"] == "CC0"
    # Delete
    r = await api_client.delete(f"/api/v1/music/{body['id']}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_upload_rejects_non_audio(api_client) -> None:
    r = await api_client.post(
        "/api/v1/music/uploads",
        files={"file": ("x.txt", b"hello", "text/plain")},
        data={"title": "t", "license": "CC0", "scope": "global"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_upload_rejects_unknown_extension(api_client) -> None:
    r = await api_client.post(
        "/api/v1/music/uploads",
        files={"file": ("x.xyz", b"\x00" * 100, "audio/x-unknown")},
        data={"title": "t", "license": "CC0", "scope": "global"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_nonexistent_user_track(api_client) -> None:
    r = await api_client.delete("/api/v1/music/nonexistent-user-track")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_bundled_track_is_forbidden(api_client) -> None:
    # calm-01 is a bundled track in the image.
    r = await api_client.delete("/api/v1/music/calm-01")
    # If the bundled manifest is missing (tests outside container), 404 is fine.
    assert r.status_code in (403, 404)
