"""CP3: an agent asks for clips once and polls one job."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api import db as dbmod


async def _mint(api_client) -> str:
    return (await api_client.post("/api/v1/api-keys", json={"name": "Muse"})).json()["token"]


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


def _ranked(candidate_id: str, title: str, overall: float, rank: int = 1):
    """A REAL RankedReel: a stand-in with invented attribute names let a wrong
    field ('overall_score') pass tests and fail on live footage."""
    from reelforge_core.models import RankedReel, ReelScores

    return RankedReel(
        candidate_id=candidate_id,
        scene_indices=[0],
        start_sec=0.0,
        end_sec=20.0,
        duration_sec=20.0,
        title=title,
        hook="hook",
        justification="j",
        scores=ReelScores(
            narrative_coherence=70, hook_strength=70,
            emotional_payoff=70, standalone_clarity=70,
        ),
        overall=overall,
        rank=rank,
        suggested_mood="neutral",
    )


async def _project_with_clips(api_client, isolated_data_dir: Path, n: int) -> str:
    """A project whose asset rows point at files that exist on disk."""
    pid = (await api_client.post("/api/v1/projects", json={"name": "batch"})).json()["id"]
    uploads = isolated_data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    async with dbmod.db_state.sessionmaker() as db:
        for i in range(n):
            path = uploads / f"clip{i}.mp4"
            path.write_bytes(b"\0" * 32)
            db.add(
                dbmod.Asset(
                    id=f"{'a' * 63}{i}",
                    project_id=pid,
                    kind="video",
                    path=str(path),
                    original_filename=f"IMG_000{i}.mov",
                    duration_sec=30.0 + i,
                    width=1920,
                    height=1080,
                    fps=30.0,
                    has_audio=True,
                    size_bytes=32,
                    probe_json="{}",
                )
            )
        await db.commit()
    return pid


# --- enqueue contracts ------------------------------------------------------------


async def test_cut_reels_queues_one_job_and_returns_its_id(
    api_client, isolated_data_dir: Path
) -> None:
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 2)

    _, data = await _call(
        api_client, token, "cut_reels", {"project_id": pid, "count": 2, "direction": "the fun bits"}
    )
    assert data["status"] == "queued"
    job_id = data["jobId"]

    async with dbmod.db_state.sessionmaker() as db:
        row = await db.get(dbmod.Job, job_id)
    assert row.kind == "agent_cut" and row.project_id == pid
    args = json.loads(row.config_json)
    assert args["mode"] == "reels" and args["count"] == 2
    assert args["prompt"] == "the fun bits"


async def test_only_one_cut_per_project_at_a_time(
    api_client, isolated_data_dir: Path
) -> None:
    """Cutting costs tokens and tens of minutes; a confused agent must not be
    able to queue ten runs of it."""
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 1)

    _, first = await _call(api_client, token, "cut_reels", {"project_id": pid})
    assert first["status"] == "queued"

    result, text = await _call(api_client, token, "cut_reels", {"project_id": pid})
    assert result["isError"] is True
    assert "already queued or running" in text


async def test_cut_refuses_an_empty_or_unknown_project(api_client) -> None:
    token = await _mint(api_client)
    empty = (await api_client.post("/api/v1/projects", json={"name": "nothing here"})).json()

    result, text = await _call(api_client, token, "cut_reels", {"project_id": empty["id"]})
    assert result["isError"] and "send footage first" in text

    result, text = await _call(api_client, token, "cut_reels", {"project_id": "nope"})
    assert result["isError"] and "not found" in text

    result, text = await _call(api_client, token, "cut_reels", {})
    assert result["isError"] and "project_id is required" in text


async def test_long_video_needs_more_than_one_clip(
    api_client, isolated_data_dir: Path
) -> None:
    token = await _mint(api_client)
    one = await _project_with_clips(api_client, isolated_data_dir, 1)
    result, text = await _call(api_client, token, "make_long_video", {"project_id": one})
    assert result["isError"] and "mixed from several clips" in text


async def test_long_video_creates_the_mix_row_it_will_render_into(
    api_client, isolated_data_dir: Path
) -> None:
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 3)
    # The mix pipeline loads the primary clip's probe.json.
    from reelforge_core.analysis.pipeline import working_dir_for

    for i in range(3):
        wd = working_dir_for(f"{'a' * 63}{i}")
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "probe.json").write_text("{}")

    _, data = await _call(
        api_client, token, "make_long_video", {"project_id": pid, "target_duration_sec": 420}
    )
    assert data["status"] == "queued"

    async with dbmod.db_state.sessionmaker() as db:
        row = await db.get(dbmod.Job, data["jobId"])
        mix = await db.get(dbmod.Reel, row.reel_id)
    assert row.reel_id.startswith("mix-")
    assert mix is not None and mix.duration_sec == 420
    # Primary is the longest clip — its working dir hosts the render.
    assert row.asset_id == f"{'a' * 63}2"


async def test_cut_rejects_out_of_range_options(api_client, isolated_data_dir: Path) -> None:
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 2)

    result, text = await _call(api_client, token, "cut_reels", {"project_id": pid, "count": 99})
    assert result["isError"] and "count" in text

    result, text = await _call(
        api_client, token, "make_long_video", {"project_id": pid, "target_duration_sec": 5}
    )
    assert result["isError"] and "target_duration_sec" in text


async def test_asset_rows_whose_file_vanished_are_not_sent_to_the_worker(
    api_client, isolated_data_dir: Path
) -> None:
    """Failing minutes into a run is worse than refusing up front."""
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 1)
    (isolated_data_dir / "uploads" / "clip0.mp4").unlink()

    result, text = await _call(api_client, token, "cut_reels", {"project_id": pid})
    assert result["isError"] and "send footage first" in text


# --- polling ---------------------------------------------------------------------


async def test_check_job_speaks_in_stages_not_jargon(
    api_client, isolated_data_dir: Path
) -> None:
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 1)
    _, started = await _call(api_client, token, "cut_reels", {"project_id": pid})

    async with dbmod.db_state.sessionmaker() as db:
        row = await db.get(dbmod.Job, started["jobId"])
        row.status = "running"
        row.stage = "transcribe"
        row.progress = 0.42
        await db.commit()

    _, data = await _call(api_client, token, "check_job", {"job_id": started["jobId"]})
    assert data["status"] == "running"
    assert data["percent"] == 42
    assert data["doing"] == "listening to what's said"


async def test_check_job_returns_the_clips_when_it_finishes(
    api_client, isolated_data_dir: Path
) -> None:
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 1)
    _, started = await _call(api_client, token, "cut_reels", {"project_id": pid})

    finished = {
        "projectId": pid,
        "clips": [
            {"clipId": "abc123", "title": "The wipeout", "durationSec": 22.5,
             "exportPath": "/data/outputs/x/abc123/mp4_h264_social.mp4"}
        ],
        "elapsedSec": 512.3,
    }
    async with dbmod.db_state.sessionmaker() as db:
        row = await db.get(dbmod.Job, started["jobId"])
        row.status = "done"
        row.progress = 1.0
        row.stage = "done"
        row.result_json = json.dumps(finished)
        await db.commit()

    _, data = await _call(api_client, token, "check_job", {"job_id": started["jobId"]})
    assert data["status"] == "done" and data["percent"] == 100
    assert data["clips"][0]["title"] == "The wipeout"
    assert data["elapsedSec"] == 512.3


async def test_check_job_surfaces_failure_text(api_client, isolated_data_dir: Path) -> None:
    token = await _mint(api_client)
    pid = await _project_with_clips(api_client, isolated_data_dir, 1)
    _, started = await _call(api_client, token, "cut_reels", {"project_id": pid})

    async with dbmod.db_state.sessionmaker() as db:
        row = await db.get(dbmod.Job, started["jobId"])
        row.status = "failed"
        row.error_message = "none of this footage could be read"
        await db.commit()

    _, data = await _call(api_client, token, "check_job", {"job_id": started["jobId"]})
    assert data["status"] == "failed"
    assert "none of this footage could be read" in data["error"]


async def test_check_job_needs_a_real_id(api_client) -> None:
    token = await _mint(api_client)
    result, text = await _call(api_client, token, "check_job", {})
    assert result["isError"] and "job_id is required" in text

    result, text = await _call(api_client, token, "check_job", {"job_id": "made-up"})
    assert result["isError"] and "not found" in text


# --- the orchestration itself -------------------------------------------------------


async def test_cut_reels_reuses_existing_analysis_and_ranks_across_clips(
    isolated_data_dir: Path, monkeypatch
) -> None:
    """The order of operations, without running ffmpeg or Whisper: analysis is
    reused when present, selection runs per clip, and the clips actually
    rendered are the best across the whole batch."""
    from apps.worker import agent_cut

    analyzed: list[str] = []
    composed: list[str] = []

    def _fake_probe(path):
        from types import SimpleNamespace

        return SimpleNamespace(id=Path(path).stem, path=path)

    async def _fake_analyze(asset, config):
        analyzed.append(asset.id)
        return f"report-for-{asset.id}"

    async def _fake_select(report, config):
        from types import SimpleNamespace

        # Two candidates per clip, with scores that interleave across clips.
        asset_id = str(report).replace("report-for-", "")
        base = 90.0 if asset_id == "clipB" else 50.0
        return SimpleNamespace(
            reels=[
                _ranked(f"{asset_id}-{n}", f"{asset_id} {n}", base - n, rank=n + 1)
                for n in range(2)
            ]
        )

    async def _fake_compose(asset, reel, report, config):
        composed.append(reel.candidate_id)
        return None

    async def _fake_export(asset_id, reel_id, preset):
        from types import SimpleNamespace

        out = isolated_data_dir / f"{reel_id}.mp4"
        out.write_bytes(b"\0" * 10)
        return SimpleNamespace(output_path=str(out))

    monkeypatch.setattr(agent_cut, "probe", _fake_probe)
    monkeypatch.setattr(agent_cut, "analyze", _fake_analyze)
    monkeypatch.setattr(agent_cut, "select_reels", _fake_select)
    monkeypatch.setattr(agent_cut, "compose", _fake_compose)
    monkeypatch.setattr(agent_cut, "export", _fake_export)
    monkeypatch.setattr(
        agent_cut, "AnalysisReport",
        type("R", (), {"model_validate_json": staticmethod(lambda t: t)}),
    )

    # clipA already analyzed; clipB not.
    from reelforge_core.analysis.pipeline import working_dir_for

    wd = working_dir_for("clipA")
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "analysis.json").write_text("report-for-clipA")

    events: list = []

    async def _progress(ev):
        events.append(ev)

    result = await agent_cut.cut_reels(
        agent_cut.CutRequest(
            project_id="p1",
            sources=[("clipA", str(isolated_data_dir / "clipA.mp4"), "A.mov"),
                     ("clipB", str(isolated_data_dir / "clipB.mp4"), "B.mov")],
            top_k=2,
        ),
        _progress,
    )

    assert analyzed == ["clipB"]  # clipA's analysis was reused
    # Both of clipB's candidates outscore clipA's, so those are what render.
    assert composed == ["clipB-0", "clipB-1"]
    assert [c["title"] for c in result["clips"]] == ["clipB 0", "clipB 1"]
    assert result["clips"][0]["bytes"] == 10
    assert events and events[-1].overall_progress == 1.0


async def test_one_unreadable_clip_does_not_sink_the_batch(
    isolated_data_dir: Path, monkeypatch
) -> None:
    from apps.worker import agent_cut

    def _fake_probe(path):
        from types import SimpleNamespace

        if "bad" in str(path):
            raise RuntimeError("moov atom not found")
        return SimpleNamespace(id=Path(path).stem, path=path)

    async def _fake_analyze(asset, config):
        return f"report-for-{asset.id}"

    async def _fake_select(report, config):
        from types import SimpleNamespace

        return SimpleNamespace(reels=[_ranked("ok-0", "fine", 70.0)])

    async def _fake_compose(asset, reel, report, config):
        return None

    async def _fake_export(asset_id, reel_id, preset):
        from types import SimpleNamespace

        out = isolated_data_dir / f"{reel_id}.mp4"
        out.write_bytes(b"\0")
        return SimpleNamespace(output_path=str(out))

    monkeypatch.setattr(agent_cut, "probe", _fake_probe)
    monkeypatch.setattr(agent_cut, "analyze", _fake_analyze)
    monkeypatch.setattr(agent_cut, "select_reels", _fake_select)
    monkeypatch.setattr(agent_cut, "compose", _fake_compose)
    monkeypatch.setattr(agent_cut, "export", _fake_export)

    async def _progress(ev):
        return None

    result = await agent_cut.cut_reels(
        agent_cut.CutRequest(
            project_id="p1",
            sources=[("bad", str(isolated_data_dir / "bad.mp4"), "bad.mov"),
                     ("ok", str(isolated_data_dir / "ok.mp4"), "ok.mov")],
            top_k=3,
        ),
        _progress,
    )
    assert len(result["clips"]) == 1


async def test_a_direction_that_matches_nothing_says_so(
    isolated_data_dir: Path, monkeypatch
) -> None:
    from apps.worker import agent_cut

    def _fake_probe(path):
        from types import SimpleNamespace

        return SimpleNamespace(id="clip", path=path)

    async def _fake_analyze(asset, config):
        return "report"

    async def _fake_select(report, config):
        from types import SimpleNamespace

        return SimpleNamespace(reels=[])  # the relevance gate dropped everything

    monkeypatch.setattr(agent_cut, "probe", _fake_probe)
    monkeypatch.setattr(agent_cut, "analyze", _fake_analyze)
    monkeypatch.setattr(agent_cut, "select_reels", _fake_select)

    async def _progress(ev):
        return None

    result = await agent_cut.cut_reels(
        agent_cut.CutRequest(
            project_id="p1",
            sources=[("clip", str(isolated_data_dir / "clip.mp4"), "c.mov")],
            prompt="anything about quantum mechanics",
        ),
        _progress,
    )
    assert result["clips"] == []
    assert "broader wording" in result["note"]


async def test_no_readable_footage_fails_loudly(isolated_data_dir: Path, monkeypatch) -> None:
    from apps.worker import agent_cut

    def _fake_probe(path):
        raise RuntimeError("not media")

    monkeypatch.setattr(agent_cut, "probe", _fake_probe)

    async def _progress(ev):
        return None

    with pytest.raises(RuntimeError, match="none of this footage could be read"):
        await agent_cut.cut_reels(
            agent_cut.CutRequest(
                project_id="p1",
                sources=[("clip", str(isolated_data_dir / "clip.mp4"), "c.mov")],
            ),
            _progress,
        )
