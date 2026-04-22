"""Range request helper + reel preview + export download."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write(p: Path, size: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        f.write(b"A" * size)


async def _seed_reel_with_mezz(
    api_client, isolated_data_dir: Path, size: int = 2048
) -> tuple[str, Path]:
    from apps.api import db as dbmod
    from reelforge_core.analysis.pipeline import working_dir_for

    r = await api_client.post("/api/v1/projects", json={"name": "media"})
    pid = r.json()["id"]
    aid = "f" * 64
    rid = "reel-media-1"
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id=aid,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=30,
                width=640,
                height=360,
                fps=25,
                has_audio=True,
                size_bytes=1,
                probe_json="{}",
            )
        )
        session.add(
            dbmod.Reel(
                id=rid,
                project_id=pid,
                asset_id=aid,
                rank=1,
                title="media-reel",
                hook="h",
                justification="j",
                start_sec=0.0,
                end_sec=30.0,
                duration_sec=30.0,
                overall_score=70,
                suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
            )
        )
        await session.commit()
    mezz = working_dir_for(aid) / "reels" / rid / "mezzanine.mp4"
    _write(mezz, size)
    return rid, mezz


@pytest.mark.asyncio
async def test_preview_missing_reel_404(api_client) -> None:
    r = await api_client.get("/api/v1/reels/does-not-exist/preview")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_preview_mezzanine_not_ready(api_client) -> None:
    # Seed a reel without a mezzanine
    from apps.api import db as dbmod

    r = await api_client.post("/api/v1/projects", json={"name": "no-mezz"})
    pid = r.json()["id"]
    async with dbmod.db_state.sessionmaker() as session:
        session.add(
            dbmod.Asset(
                id="g" * 64,
                project_id=pid,
                path="/tmp/x.mp4",
                original_filename="x.mp4",
                duration_sec=1,
                width=100,
                height=100,
                fps=25,
                has_audio=False,
                size_bytes=1,
                probe_json="{}",
            )
        )
        session.add(
            dbmod.Reel(
                id="no-mezz-reel",
                project_id=pid,
                asset_id="g" * 64,
                rank=1,
                title="x",
                hook="x",
                justification="j",
                start_sec=0,
                end_sec=30,
                duration_sec=30,
                overall_score=70,
                suggested_mood="neutral",
                scene_indices_json="[0]",
                scores_json='{"narrative_coherence":70,"hook_strength":70,"emotional_payoff":70,"standalone_clarity":70}',
            )
        )
        await session.commit()

    r = await api_client.get("/api/v1/reels/no-mezz-reel/preview")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "MEZZANINE_NOT_READY"


@pytest.mark.asyncio
async def test_preview_full_body(api_client, isolated_data_dir: Path) -> None:
    rid, mezz = await _seed_reel_with_mezz(api_client, isolated_data_dir, size=1024)
    r = await api_client.get(f"/api/v1/reels/{rid}/preview")
    assert r.status_code == 200
    assert "video/mp4" in r.headers.get("content-type", "")
    assert r.headers.get("accept-ranges") == "bytes"
    assert len(r.content) == 1024


@pytest.mark.asyncio
async def test_preview_range_partial(api_client, isolated_data_dir: Path) -> None:
    rid, mezz = await _seed_reel_with_mezz(api_client, isolated_data_dir, size=2048)
    r = await api_client.get(
        f"/api/v1/reels/{rid}/preview", headers={"Range": "bytes=100-199"}
    )
    assert r.status_code == 206
    assert len(r.content) == 100
    assert r.headers.get("content-range") == "bytes 100-199/2048"


@pytest.mark.asyncio
async def test_preview_range_unsatisfiable(api_client, isolated_data_dir: Path) -> None:
    rid, mezz = await _seed_reel_with_mezz(api_client, isolated_data_dir, size=500)
    r = await api_client.get(
        f"/api/v1/reels/{rid}/preview", headers={"Range": "bytes=1000-2000"}
    )
    assert r.status_code == 416
    assert r.headers.get("content-range") == "bytes */500"


@pytest.mark.asyncio
async def test_preview_range_suffix(api_client, isolated_data_dir: Path) -> None:
    rid, mezz = await _seed_reel_with_mezz(api_client, isolated_data_dir, size=2000)
    r = await api_client.get(
        f"/api/v1/reels/{rid}/preview", headers={"Range": "bytes=-512"}
    )
    assert r.status_code == 206
    assert len(r.content) == 512
    assert r.headers.get("content-range") == "bytes 1488-1999/2000"
