"""CP4: how finished clips get back — signed links, a synced folder, email."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.worker import delivery
from reelforge_core import links


@pytest.fixture(autouse=True)
def _isolated(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from apps.api.settings import settings

    monkeypatch.setattr(settings, "data_dir", isolated_data_dir)
    links.reset_cache()
    yield
    links.reset_cache()


def _export(isolated_data_dir: Path, reel_id: str = "abc123") -> Path:
    path = isolated_data_dir / "outputs" / "asset1" / reel_id / "mp4_h264_social.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 2048)
    return path


# --- channel choice ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ["links"]),
        ([], ["links"]),
        ("folder,email", ["folder", "email"]),
        (["email", "links"], ["links", "email"]),  # stable order
        (["nonsense"], ["links"]),  # unknown names fall back to the default
        (["links", "links"], ["links"]),
    ],
)
def test_parse_channels(raw, expected) -> None:
    assert delivery.parse_channels(raw) == expected


def test_parse_channels_honours_a_configured_default() -> None:
    assert delivery.parse_channels(None, default="folder,email") == ["folder", "email"]


# --- links -------------------------------------------------------------------------


def test_media_token_round_trips_and_stays_inside_outputs(isolated_data_dir: Path) -> None:
    path = _export(isolated_data_dir)
    token = links.media_token(path)
    assert links.resolve_media_token(token) == path.resolve()

    # A token naming something outside outputs must not resolve, however it
    # was built — this is what stops a link reaching the database.
    escape = links.sign({"p": "media", "f": "../../reelforge.db"}, 60)
    assert links.resolve_media_token(escape) is None

    # Wrong purpose, expired, and tampered all refuse.
    assert links.resolve_media_token(links.sign({"p": "upload", "f": "x"}, 60)) is None
    assert links.resolve_media_token(links.sign({"p": "media", "f": "x"}, -1)) is None
    assert links.resolve_media_token("garbage") is None


def test_media_token_for_a_missing_file_resolves_to_nothing(
    isolated_data_dir: Path,
) -> None:
    path = _export(isolated_data_dir)
    token = links.media_token(path)
    path.unlink()
    assert links.resolve_media_token(token) is None


async def test_media_route_serves_the_clip_and_refuses_bad_links(
    api_client, isolated_data_dir: Path
) -> None:
    path = _export(isolated_data_dir)
    token = links.media_token(path)

    resp = await api_client.get(f"/media/{token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert len(resp.content) == 2048

    # Range requests work, so the clip streams rather than downloading whole.
    partial = await api_client.get(f"/media/{token}", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206 and len(partial.content) == 100

    expired = links.sign({"p": "media", "f": "asset1/abc123/mp4_h264_social.mp4"}, -5)
    gone = await api_client.get(f"/media/{expired}")
    assert gone.status_code == 404 and "expired" in gone.json()["error"]


def test_add_links_attaches_a_url_per_clip(isolated_data_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("REELFORGE_PUBLIC_MEDIA_BASE", "https://reels.example.com")
    clips = [{"clipId": "abc123", "exportPath": str(_export(isolated_data_dir))}]
    delivery.add_links(clips)
    assert clips[0]["url"].startswith("https://reels.example.com/media/")
    assert clips[0]["urlExpiresInHours"] == 48


def test_add_links_survives_a_path_it_cannot_sign(isolated_data_dir: Path) -> None:
    """A clip outside outputs shouldn't blow up the whole delivery."""
    clips = [{"clipId": "x", "exportPath": "/etc/hosts"}]
    delivery.add_links(clips)
    assert "url" not in clips[0]


# --- folder ------------------------------------------------------------------------


def test_folder_delivery_copies_under_readable_names(
    isolated_data_dir: Path, tmp_path: Path
) -> None:
    clips = [
        {"clipId": "abc123", "title": "The wipeout / take 2!", "exportPath": str(_export(isolated_data_dir, "abc123"))},
        {"clipId": "def456", "title": "The wipeout / take 2!", "exportPath": str(_export(isolated_data_dir, "def456"))},
    ]
    folder = tmp_path / "synced"
    report = delivery.copy_to_folder(clips, str(folder))

    assert report["ok"] and report["count"] == 2
    names = sorted(p.name for p in folder.iterdir())
    # Punctuation stripped, and the second copy doesn't overwrite the first.
    assert names == ["The wipeout take 2 (2).mp4", "The wipeout take 2.mp4"]
    assert all(c.get("savedAs") for c in clips)


def test_folder_delivery_says_when_it_is_not_configured(isolated_data_dir: Path) -> None:
    report = delivery.copy_to_folder([{"exportPath": str(_export(isolated_data_dir))}], "")
    assert report["ok"] is False and "REELFORGE_DELIVERY_DIR" in report["detail"]


def test_safe_filename_falls_back_when_a_title_is_all_punctuation() -> None:
    assert delivery.safe_filename("///", "clip123.mp4") == "clip123.mp4"
    assert delivery.safe_filename("Skim   board  ", "x.mp4") == "Skim board.mp4"


# --- email -------------------------------------------------------------------------


def test_email_lists_every_clip_with_its_link_and_expiry() -> None:
    clips = [
        {"title": "The wipeout", "durationSec": 22.4, "url": "https://x/media/aaa"},
        {"title": "The landing", "durationSec": 31.0, "url": "https://x/media/bbb",
         "savedAs": "/synced/The landing.mp4"},
    ]
    msg = delivery.build_email(clips, "skimboard session")
    body = msg.get_content()
    assert "2 clips" in msg["Subject"] and "skimboard session" in msg["Subject"]
    assert "The wipeout (22s)" in body
    assert "https://x/media/bbb" in body
    assert "/synced/The landing.mp4" in body
    assert "48 hours" in body


def test_email_says_when_it_is_not_set_up(monkeypatch) -> None:
    for var in ("SMTP_HOST", "SMTP_FROM", "SMTP_TO", "SMTP_USER"):
        monkeypatch.delenv(var, raising=False)
    report = delivery.send_email([{"title": "x"}], "batch")
    assert report["ok"] is False and "SMTP_HOST" in report["detail"]


def test_email_failure_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_FROM", "me@example.com")
    monkeypatch.setenv("SMTP_TO", "me@example.com")

    class _Boom:
        def __init__(self, *a, **kw):
            raise OSError("connection refused")

    monkeypatch.setattr(delivery.smtplib, "SMTP", _Boom)
    report = delivery.send_email([{"title": "x"}], "batch")
    assert report["ok"] is False and "connection refused" in report["detail"]


def test_email_is_sent_through_smtp_when_configured(monkeypatch) -> None:
    sent: dict = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["user"] = user

        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_FROM", "me@example.com")
    monkeypatch.setenv("SMTP_TO", "phone@example.com")
    monkeypatch.setattr(delivery.smtplib, "SMTP", _FakeSMTP)

    report = delivery.send_email([{"title": "clip", "url": "https://x/media/a"}], "batch")
    assert report["ok"] is True and report["to"] == "phone@example.com"
    assert sent["tls"] and sent["user"] == "me@example.com"
    assert sent["to"] == "phone@example.com" and "clip" in sent["subject"]


# --- the whole delivery step ---------------------------------------------------------


def test_deliver_runs_only_the_channels_asked_for(
    isolated_data_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REELFORGE_DELIVERY_DIR", str(tmp_path / "synced"))
    clips = [{"clipId": "abc123", "title": "Clip", "exportPath": str(_export(isolated_data_dir))}]

    report = delivery.deliver(clips, ["links"], "batch")
    assert report["links"]["ok"] and "folder" not in report and "email" not in report
    assert not (tmp_path / "synced").exists()

    report = delivery.deliver(clips, ["links", "folder"], "batch")
    assert report["folder"]["count"] == 1
    assert (tmp_path / "synced" / "Clip.mp4").exists()


def test_deliver_with_no_clips_does_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REELFORGE_DELIVERY_DIR", str(tmp_path / "synced"))
    report = delivery.deliver([], ["links", "folder", "email"], "batch")
    assert report == {"channels": ["links", "folder", "email"]}
    assert not (tmp_path / "synced").exists()


# --- through the tool ------------------------------------------------------------------


async def _mint(api_client) -> str:
    return (await api_client.post("/api/v1/api-keys", json={"name": "Muse"})).json()["token"]


async def _call(api_client, token, name, arguments=None):
    resp = await api_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments or {}}},
        headers={"Authorization": f"Bearer {token}"},
    )
    result = resp.json()["result"]
    if result["isError"]:
        return result, result["content"][0]["text"]
    return result, json.loads(result["content"][0]["text"])


async def test_delivery_choice_reaches_the_job(api_client, isolated_data_dir: Path) -> None:
    from apps.api import db as dbmod

    pid = (await api_client.post("/api/v1/projects", json={"name": "batch"})).json()["id"]
    uploads = isolated_data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / "clip.mp4"
    path.write_bytes(b"\0")
    async with dbmod.db_state.sessionmaker() as db:
        db.add(dbmod.Asset(
            id="c" * 64, project_id=pid, kind="video", path=str(path),
            original_filename="IMG.mov", duration_sec=30.0, width=1920, height=1080,
            fps=30.0, has_audio=True, size_bytes=1, probe_json="{}",
        ))
        await db.commit()

    token = await _mint(api_client)
    _, data = await _call(
        api_client, token, "cut_reels",
        {"project_id": pid, "delivery": ["folder", "email"]},
    )
    async with dbmod.db_state.sessionmaker() as db:
        row = await db.get(dbmod.Job, data["jobId"])
    assert json.loads(row.config_json)["delivery"] == ["folder", "email"]


async def test_tool_refuses_an_unknown_delivery_channel(
    api_client, isolated_data_dir: Path
) -> None:
    token = await _mint(api_client)
    result, text = await _call(
        api_client, token, "cut_reels", {"project_id": "x", "delivery": ["carrier pigeon"]}
    )
    assert result["isError"] and "links, folder or email" in text
