"""Voiceover take upload + timeline validation + compose path resolution."""

from __future__ import annotations

import io
import json
import wave

import pytest


def _wav_bytes(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x10" * int(16000 * seconds))
    return buf.getvalue()


async def _project_with_reel(api_client) -> tuple[str, str, str]:
    import apps.api.settings as settings_mod
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "vo"})
    pid = r.json()["id"]
    vid = "d" * 64
    up = settings_mod.settings.data_dir / "uploads" / f"{vid}.mp4"
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_bytes(b"v")
    async with dbmod.db_state.sessionmaker() as session:
        session.add(dbmod.Asset(
            id=vid, project_id=pid, path=str(up), original_filename="clip.mp4",
            duration_sec=60, width=1920, height=1080, fps=30, has_audio=True,
            size_bytes=1, probe_json="{}",
        ))
        session.add(dbmod.Reel(
            id="reel-vo-1", project_id=pid, asset_id=vid, rank=1, title="t", hook="h",
            justification="j", start_sec=0.0, end_sec=30.0, duration_sec=30.0,
            overall_score=80, suggested_mood="neutral", scene_indices_json="[0]",
            scores_json='{"narrative_coherence":80,"hook_strength":80,"emotional_payoff":80,"standalone_clarity":80}',
        ))
        await session.commit()
    return pid, vid, "reel-vo-1"


@pytest.mark.asyncio
async def test_upload_voiceover_creates_audio_asset(api_client) -> None:
    pid, _, _ = await _project_with_reel(api_client)
    r = await api_client.post(
        f"/api/v1/projects/{pid}/voiceovers",
        files={"file": ("take.wav", _wav_bytes(1.5), "audio/wav")},
        data={"label": "intro take"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "audio"
    assert body["original_filename"].startswith("intro take")
    assert body["duration_sec"] == pytest.approx(1.5, abs=0.05)
    assert body["width"] == 0 and body["has_audio"] is True

    # Served through the media endpoint as audio.
    media = await api_client.get(f"/api/v1/assets/{body['id']}/media")
    assert media.status_code == 200
    assert media.headers["content-type"].startswith("audio/wav")

    # Listed as a source for the editor.
    edit = (await api_client.get("/api/v1/reels/reel-vo-1/edit")).json()
    assert edit["audios"][0]["asset_id"] == body["id"]


@pytest.mark.asyncio
async def test_upload_voiceover_rejects_garbage(api_client) -> None:
    pid, _, _ = await _project_with_reel(api_client)
    r = await api_client.post(
        f"/api/v1/projects/{pid}/voiceovers",
        files={"file": ("take.webm", b"not audio at all", "audio/webm")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UPLOAD_UNSUPPORTED_TYPE"


@pytest.mark.asyncio
async def test_timeline_voiceovers_validated_and_resolved(api_client) -> None:
    pid, vid, reel_id = await _project_with_reel(api_client)
    up = await api_client.post(
        f"/api/v1/projects/{pid}/voiceovers",
        files={"file": ("take.wav", _wav_bytes(2.0), "audio/wav")},
    )
    take_id = up.json()["id"]

    base_shot = {"kind": "video", "asset_id": vid, "in_ts": 0, "out_ts": 20, "volume": 0.6, "muted": False}
    # Happy path: muted shot + a take.
    r = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [base_shot, {**base_shot, "in_ts": 20, "out_ts": 30, "muted": True}],
                           "voiceovers": [{"id": "t1", "asset_id": take_id, "start_sec": 3.0, "duration_sec": 2.0, "volume": 1.2, "muted": False, "label": "take 1"}]}},
    )
    assert r.status_code == 200, r.text
    saved = r.json()["timeline"]
    assert saved["shots"][1]["muted"] is True and saved["shots"][0]["volume"] == 0.6
    assert saved["voiceovers"][0]["path"] == ""

    # Take from a non-audio asset -> rejected.
    bad = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [base_shot],
                           "voiceovers": [{"id": "x", "asset_id": vid, "start_sec": 0, "duration_sec": 1, "volume": 1, "muted": False, "label": ""}]}},
    )
    assert bad.status_code == 400 and "audio take" in bad.json()["error"]["message"]
    # Take past the end -> rejected.
    late = await api_client.put(
        f"/api/v1/reels/{reel_id}/edit",
        json={"timeline": {"shots": [base_shot],
                           "voiceovers": [{"id": "x", "asset_id": take_id, "start_sec": 99, "duration_sec": 1, "volume": 1, "muted": False, "label": ""}]}},
    )
    assert late.status_code == 400

    # Compose resolves the take's on-disk path.
    from apps.api import db as dbmod
    from sqlalchemy import select

    c = await api_client.post(f"/api/v1/reels/{reel_id}/compose", json={"captions": {"mode": "off"}})
    assert c.status_code == 200, c.text
    async with dbmod.db_state.sessionmaker() as session:
        job = (await session.execute(
            select(dbmod.Job).where(dbmod.Job.kind == "compose").order_by(dbmod.Job.created_at.desc())
        )).scalars().first()
    cfg = json.loads(job.config_json)
    assert cfg["timeline"]["voiceovers"][0]["path"].endswith(".wav")
    assert cfg["timeline"]["shots"][1]["muted"] is True


@pytest.mark.asyncio
async def test_waveform_peaks_for_asset_range(api_client) -> None:
    """The editor draws waveforms from server-side peak envelopes."""
    import io as _io
    import wave as _wave

    pid, _, _ = await _project_with_reel(api_client)
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        loud = (b"\x00\x40" * 80 + b"\x00\xc0" * 80) * 100  # 1s of 100 Hz square at +/-16384
        w.writeframes(loud + b"\x00\x00" * 16000)  # + 1s silence
    up = await api_client.post(
        f"/api/v1/projects/{pid}/voiceovers",
        files={"file": ("take.wav", buf.getvalue(), "audio/wav")},
    )
    aid = up.json()["id"]

    r = await api_client.get(f"/api/v1/assets/{aid}/waveform?buckets=20")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["peaks"]) == 20
    assert body["end"] == pytest.approx(2.0, abs=0.05)
    first, second = body["peaks"][:10], body["peaks"][10:]
    assert max(first) > 0.5
    assert max(second[1:]) < 0.05  # bucket 10 straddles the 1.0 s edge (resampler tail)

    r2 = await api_client.get(f"/api/v1/assets/{aid}/waveform?start=1.1&end=2.0&buckets=8")
    assert r2.status_code == 200 and max(r2.json()["peaks"]) < 0.05

    bad = await api_client.get(f"/api/v1/assets/{aid}/waveform?start=1.5&end=1.0")
    assert bad.status_code == 400
