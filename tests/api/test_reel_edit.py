"""Editable timeline endpoints + compose injection."""

from __future__ import annotations

import json

import pytest


async def _seed(api_client, *, with_photo: bool = True) -> tuple[str, str, str, str | None]:
    import apps.api.settings as settings_mod
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "edit-test"})
    pid = r.json()["id"]
    vid = "a" * 64
    photo = "b" * 64 if with_photo else None
    data_dir = settings_mod.settings.data_dir
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads" / f"{vid}.mp4").write_bytes(b"v")
    if photo:
        (data_dir / "uploads" / f"{photo}.jpg").write_bytes(b"p")
    # A minimal analysis.json so the default timeline resolves scene bounds.
    wd = data_dir / "working" / vid
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "analysis.json").write_text(
        json.dumps(
            {
                "asset_id": vid, "source_path": "/x", "duration": 120.0,
                "width": 1920, "height": 1080, "fps": 30, "has_audio": True,
                "config": {}, "scenes": [
                    {"index": 0, "start_sec": 0.0, "end_sec": 20.0, "start_frame": 0, "end_frame": 600, "thumbnail_path": "thumbs/scene_0000.jpg"},
                    {"index": 1, "start_sec": 20.0, "end_sec": 50.0, "start_frame": 600, "end_frame": 1500, "thumbnail_path": "thumbs/scene_0001.jpg"},
                ],
                "transcript": None, "loudness": [], "semantics": [],
                "created_at": "2026-01-01T00:00:00+00:00", "elapsed_sec": 0.0,
                "reelforge_version": "0.5.0", "anthropic_usage": {},
            }
        )
    )
    async with dbmod.db_state.sessionmaker() as session:
        session.add(dbmod.Asset(
            id=vid, project_id=pid, path=str(data_dir / "uploads" / f"{vid}.mp4"),
            original_filename="clip.mp4", duration_sec=120, width=1920, height=1080,
            fps=30, has_audio=True, size_bytes=1, probe_json="{}",
        ))
        if photo:
            session.add(dbmod.Asset(
                id=photo, project_id=pid, kind="photo",
                path=str(data_dir / "uploads" / f"{photo}.jpg"),
                original_filename="beach.jpg", duration_sec=0, width=4000, height=3000,
                fps=0, has_audio=False, size_bytes=1, probe_json="{}",
            ))
        session.add(dbmod.Reel(
            id="reel-edit-1", project_id=pid, asset_id=vid, rank=1, title="t", hook="h",
            justification="j", start_sec=0.0, end_sec=50.0, duration_sec=50.0,
            overall_score=80, suggested_mood="neutral", scene_indices_json="[0,1]",
            scores_json='{"narrative_coherence":80,"hook_strength":80,"emotional_payoff":80,"standalone_clarity":80}',
            trim_start_offset_sec=1.0,
        ))
        await session.commit()
    return pid, vid, "reel-edit-1", photo


@pytest.mark.asyncio
async def test_get_edit_returns_ai_cut_as_default_timeline(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    r = await api_client.get(f"/api/v1/reels/{reel_id}/edit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_edits"] is False
    shots = body["timeline"]["shots"]
    assert [s["kind"] for s in shots] == ["video", "video"]
    # Scene bounds, with the saved +1.0 start trim folded into shot 0.
    assert shots[0]["in_ts"] == pytest.approx(1.0) and shots[0]["out_ts"] == 20.0
    assert shots[1]["in_ts"] == 20.0 and shots[1]["out_ts"] == 50.0
    assert all(s["path"] == "" for s in shots), "paths must never reach the client"
    # Sources: the video with its scenes, and the photo.
    assert body["videos"][0]["asset_id"] == vid
    assert len(body["videos"][0]["scenes"]) == 2
    assert body["photos"][0]["asset_id"] == photo


@pytest.mark.asyncio
async def test_put_edit_saves_and_reel_reports_edits(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    timeline = {
        "shots": [
            {"kind": "photo", "asset_id": photo, "duration_sec": 2.5},
            {"kind": "video", "asset_id": vid, "in_ts": 5.0, "out_ts": 15.0,
             "transition_after": {"kind": "slideleft", "duration_sec": 0.8}},
            {"kind": "video", "asset_id": vid, "in_ts": 40.0, "out_ts": 48.0},
        ],
        "overlays": [
            {"text": "Day one", "start_sec": 0.5, "end_sec": 3.0, "position": "top"},
        ],
    }
    r = await api_client.put(f"/api/v1/reels/{reel_id}/edit", json={"timeline": timeline})
    assert r.status_code == 200, r.text
    assert r.json()["has_edits"] is True

    reel = (await api_client.get(f"/api/v1/reels/{reel_id}")).json()
    assert reel["has_edits"] is True
    assert reel["edited_duration_sec"] == pytest.approx(2.5 + 10 + 8)

    again = (await api_client.get(f"/api/v1/reels/{reel_id}/edit")).json()
    assert again["has_edits"] is True
    assert again["timeline"]["shots"][1]["transition_after"]["kind"] == "slideleft"
    assert again["timeline"]["overlays"][0]["text"] == "Day one"


@pytest.mark.asyncio
async def test_put_edit_rejects_foreign_or_mismatched_assets(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    # A photo asset used as a video shot.
    r = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [{"kind": "video", "asset_id": photo, "in_ts": 0, "out_ts": 5}]}},
    )
    assert r.status_code == 400 and "photo" in r.json()["error"]["message"]
    # An asset from nowhere.
    r = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [{"kind": "video", "asset_id": "z" * 64, "in_ts": 0, "out_ts": 5}]}},
    )
    assert r.status_code == 400
    # Out of range.
    r = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [{"kind": "video", "asset_id": vid, "in_ts": 100, "out_ts": 130}]}},
    )
    assert r.status_code == 400 and "past the end" in r.json()["error"]["message"]
    # Overlay past the end of the reel.
    r = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {
            "shots": [{"kind": "video", "asset_id": vid, "in_ts": 0, "out_ts": 5}],
            "overlays": [{"text": "x", "start_sec": 50, "end_sec": 52}],
        }},
    )
    assert r.status_code == 400 and "starts at" in r.json()["error"]["message"]
    # Empty timeline.
    r = await api_client.put(f"/api/v1/reels/{reel_id}/edit", json={"timeline": {"shots": []}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_edit_resets_to_ai_cut(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [{"kind": "video", "asset_id": vid, "in_ts": 0, "out_ts": 5}]}},
    )
    r = await api_client.delete(f"/api/v1/reels/{reel_id}/edit")
    assert r.status_code == 204
    body = (await api_client.get(f"/api/v1/reels/{reel_id}/edit")).json()
    assert body["has_edits"] is False and len(body["timeline"]["shots"]) == 2


@pytest.mark.asyncio
async def test_compose_injects_saved_timeline_with_paths(api_client) -> None:
    pid, vid, reel_id, photo = await _seed(api_client)
    from apps.api import db as dbmod
    from sqlalchemy import select

    await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [
            {"kind": "photo", "asset_id": photo, "duration_sec": 3},
            {"kind": "video", "asset_id": vid, "in_ts": 0, "out_ts": 5},
        ]}},
    )
    r = await api_client.post(f"/api/v1/reels/{reel_id}/compose", json={"captions": {"mode": "off"}})
    assert r.status_code == 200, r.text
    async with dbmod.db_state.sessionmaker() as session:
        job = (await session.execute(
            select(dbmod.Job).where(dbmod.Job.kind == "compose").order_by(dbmod.Job.created_at.desc())
        )).scalars().first()
    cfg = json.loads(job.config_json)
    shots = cfg["timeline"]["shots"]
    assert shots[0]["path"].endswith(".jpg") and shots[1]["path"].endswith(".mp4")
    assert cfg.get("photo_inserts", []) == []

    # ignore_edits renders the AI cut: no timeline in the job config.
    r2 = await api_client.post(
        f"/api/v1/reels/{reel_id}/compose", json={"ignore_edits": True, "captions": {"mode": "off"}}
    )
    # Conflict detection: the first compose is still queued.
    assert r2.status_code in (200, 409)
