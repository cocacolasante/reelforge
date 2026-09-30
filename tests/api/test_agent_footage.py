"""CP2: getting footage in — the phone upload link and the watch folder."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from reelforge_core import links as signing


@pytest.fixture(autouse=True)
def _fresh_signing_secret(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test gets its own data dir, so the cached secret must not carry —
    and the secret must be written there, never into the real /data."""
    from apps.api.settings import settings

    monkeypatch.setattr(settings, "data_dir", isolated_data_dir)
    signing.reset_cache()
    yield
    signing.reset_cache()


def _tiny_video(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=size=160x120:rate=15:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


async def _mint(api_client) -> str:
    resp = await api_client.post("/api/v1/api-keys", json={"name": "Muse"})
    return resp.json()["token"]


async def _call(api_client, token, name, arguments=None):
    resp = await api_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments or {}}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    if result["isError"]:
        return result, result["content"][0]["text"]
    return result, json.loads(result["content"][0]["text"])


# --- signed links ---------------------------------------------------------------


def test_tokens_carry_their_payload_and_expire(isolated_data_dir: Path) -> None:
    token = signing.sign({"p": "upload", "project": "abc"}, 60)
    assert signing.verify(token)["project"] == "abc"

    assert signing.verify(signing.sign({"p": "upload"}, -1)) is None  # already expired
    assert signing.verify("nonsense") is None
    assert signing.verify("") is None

    # A tampered payload fails the signature rather than being trusted.
    body, mac = token.split(".", 1)
    other = signing.sign({"p": "upload", "project": "someone-elses"}, 60).split(".", 1)[0]
    assert signing.verify(f"{other}.{mac}") is None


def test_secret_survives_a_restart(isolated_data_dir: Path) -> None:
    token = signing.sign({"p": "upload", "project": "abc"}, 60)
    signing.reset_cache()  # as if the API had restarted
    assert signing.verify(token) is not None
    assert (isolated_data_dir / ".link_secret").exists()


# --- the upload link ------------------------------------------------------------


async def test_start_upload_makes_a_project_and_a_tappable_link(api_client) -> None:
    token = await _mint(api_client)
    _, data = await _call(api_client, token, "start_upload", {"name": "skimboard session"})
    assert data["project_name"] == "skimboard session"
    assert "/upload/" in data["upload_url"]
    assert data["expires_in_seconds"] > 0

    # The page names the project so the user knows where the clips are going.
    link_token = data["upload_url"].split("/upload/", 1)[1]
    page = await api_client.get(f"/upload/{link_token}")
    assert page.status_code == 200
    assert "skimboard session" in page.text
    assert "Choose videos" in page.text


async def test_upload_page_refuses_a_bad_or_expired_link(api_client) -> None:
    assert (await api_client.get("/upload/not-a-token")).status_code == 404
    expired = signing.sign({"p": "upload", "project": "whatever"}, -5)
    page = await api_client.get(f"/upload/{expired}")
    assert page.status_code == 404 and "expired" in page.text

    # A token signed for something else must not open the upload page.
    wrong_purpose = signing.sign({"p": "media", "project": "whatever"}, 60)
    assert (await api_client.get(f"/upload/{wrong_purpose}")).status_code == 404


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
async def test_uploaded_file_becomes_an_asset_in_that_project(
    api_client, tmp_path: Path
) -> None:
    token = await _mint(api_client)
    _, link = await _call(api_client, token, "start_upload", {"name": "beach"})
    link_token = link["upload_url"].split("/upload/", 1)[1]

    clip = _tiny_video(tmp_path / "IMG_0001.mp4")
    resp = await api_client.post(
        f"/upload/{link_token}/file",
        files={"file": ("IMG_0001.mp4", clip.read_bytes(), "video/mp4")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "video"

    assets = (await api_client.get(f"/api/v1/projects/{link['project_id']}/assets")).json()
    assert [a["original_filename"] for a in assets["assets"]] == ["IMG_0001.mp4"]

    # And the agent can see it arrived.
    _, footage = await _call(api_client, token, "list_footage")
    batch = next(b for b in footage["batches"] if b["projectId"] == link["project_id"])
    assert batch["videoClips"] == 1
    assert batch["clips"][0]["filename"] == "IMG_0001.mp4"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
async def test_footage_already_here_is_reported_not_silently_moved(
    api_client, tmp_path: Path
) -> None:
    """An asset id is the content hash and an asset belongs to one project, so
    re-sending the same clip cannot join a second batch. Say where it is."""
    token = await _mint(api_client)
    clip = _tiny_video(tmp_path / "same.mp4")

    _, first = await _call(api_client, token, "start_upload", {"name": "first batch"})
    first_token = first["upload_url"].split("/upload/", 1)[1]
    resp = await api_client.post(
        f"/upload/{first_token}/file",
        files={"file": ("same.mp4", clip.read_bytes(), "video/mp4")},
    )
    assert resp.json()["added"] is True

    _, second = await _call(api_client, token, "start_upload", {"name": "second batch"})
    second_token = second["upload_url"].split("/upload/", 1)[1]
    again = await api_client.post(
        f"/upload/{second_token}/file",
        files={"file": ("same.mp4", clip.read_bytes(), "video/mp4")},
    )
    body = again.json()
    assert body["added"] is False
    assert "first batch" in body["note"]
    assert body["projectId"] == first["project_id"]

    # The second batch is honestly empty, not credited with a clip it lacks.
    assets = (await api_client.get(f"/api/v1/projects/{second['project_id']}/assets")).json()
    assert assets["assets"] == []


async def test_upload_rejects_a_file_that_is_not_media(api_client) -> None:
    token = await _mint(api_client)
    _, link = await _call(api_client, token, "start_upload")
    link_token = link["upload_url"].split("/upload/", 1)[1]
    resp = await api_client.post(
        f"/upload/{link_token}/file",
        files={"file": ("notes.txt", b"not a video at all", "text/plain")},
    )
    assert resp.status_code == 400
    assert "could not read" in resp.json()["error"]


async def test_upload_to_an_expired_link_is_refused(api_client) -> None:
    expired = signing.sign({"p": "upload", "project": "x"}, -5)
    resp = await api_client.post(
        f"/upload/{expired}/file", files={"file": ("a.mp4", b"\0", "video/mp4")}
    )
    assert resp.status_code == 404


# --- the watch folder -------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
async def test_watch_folder_ingests_settled_files_once(
    api_client, tmp_path: Path, monkeypatch
) -> None:
    from apps.api import db as dbmod
    from apps.api.services import watch_folder
    from apps.api.settings import settings

    watched = tmp_path / "watched"
    (watched / "skate day").mkdir(parents=True)
    monkeypatch.setattr(settings, "watch_dir", str(watched))
    monkeypatch.setattr(settings, "watch_settle_seconds", 0.0)

    loose = _tiny_video(watched / "IMG_1.mov")
    _tiny_video(watched / "skate day" / "IMG_2.mov")

    async with dbmod.db_state.sessionmaker() as db:
        assert await watch_folder.scan_once(db) == 2
        # Re-scanning takes nothing: the files are already recorded.
        assert await watch_folder.scan_once(db) == 0

    projects = (await api_client.get("/api/v1/projects")).json()["projects"]
    names = {p["name"] for p in projects}
    assert "skate day" in names  # a subfolder becomes its own project
    assert any(n.startswith("Dropped in ") for n in names)  # loose files, by day

    # The user's own file is left where it was.
    assert loose.exists()

    # A re-saved file (new mtime) comes in again.
    time.sleep(1.1)
    loose.touch()
    async with dbmod.db_state.sessionmaker() as db:
        assert await watch_folder.scan_once(db) == 1


async def test_watch_folder_skips_unsettled_files(tmp_path: Path, monkeypatch) -> None:
    from apps.api import db as dbmod
    from apps.api.services import watch_folder
    from apps.api.settings import settings

    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "half-synced.mov").write_bytes(b"\0" * 1024)
    monkeypatch.setattr(settings, "watch_dir", str(watched))
    monkeypatch.setattr(settings, "watch_settle_seconds", 3600.0)

    async with dbmod.db_state.sessionmaker() as db:
        assert await watch_folder.scan_once(db) == 0


async def test_watch_folder_ignores_non_media_and_records_failures(
    api_client, tmp_path: Path, monkeypatch
) -> None:
    from apps.api import db as dbmod
    from apps.api.services import watch_folder
    from apps.api.settings import settings
    from sqlalchemy import select

    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "notes.txt").write_text("ignored entirely")
    (watched / ".hidden.mov").write_bytes(b"\0")
    (watched / "broken.mov").write_bytes(b"not really a movie")
    monkeypatch.setattr(settings, "watch_dir", str(watched))
    monkeypatch.setattr(settings, "watch_settle_seconds", 0.0)

    async with dbmod.db_state.sessionmaker() as db:
        assert await watch_folder.scan_once(db) == 0
        rows = (await db.execute(select(dbmod.WatchIngest))).scalars().all()
    # Only the media-looking file is even attempted, and its failure is
    # remembered so the next scan doesn't retry it forever.
    assert [Path(r.path).name for r in rows] == ["broken.mov"]
    assert rows[0].error and rows[0].asset_id is None


def test_project_naming_rules(tmp_path: Path) -> None:
    from apps.api.services.watch_folder import project_name_for

    root = tmp_path
    assert project_name_for(root / "shoot" / "a.mov", root) == "shoot"
    assert project_name_for(root / "shoot" / "sub" / "a.mov", root) == "shoot"
    assert project_name_for(root / "a.mov", root).startswith("Dropped in ")


async def test_disabled_watch_folder_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    from apps.api import db as dbmod
    from apps.api.services import watch_folder
    from apps.api.settings import settings

    monkeypatch.setattr(settings, "watch_dir", "")
    async with dbmod.db_state.sessionmaker() as db:
        assert await watch_folder.scan_once(db) == 0
    monkeypatch.setattr(settings, "watch_dir", str(tmp_path / "does-not-exist"))
    async with dbmod.db_state.sessionmaker() as db:
        assert await watch_folder.scan_once(db) == 0
