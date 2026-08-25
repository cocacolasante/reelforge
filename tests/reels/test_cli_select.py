"""Smoke test for the `reelforge select` CLI via --local."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apps.cli.main import app
from reelforge_core.reels import generate_candidates

from tests.reels._fake_ranking_client import FakeRankingClient, all_rankings
from tests.reels._fixtures import make_analysis


def test_cli_select_local_happy_path(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed analysis.json on disk so the CLI can load it. asset_id must be 64 hex.
    analysis = make_analysis("a" * 64, [10.0] * 6)
    assert len(analysis.asset_id) == 64  # matches the CLI's asset_id regex
    wd = isolated_data_dir / "working" / analysis.asset_id
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "analysis.json").write_text(analysis.model_dump_json(indent=2))

    # Prepare the fake Anthropic client.
    candidates = generate_candidates(
        analysis, config=__import__("reelforge_core", fromlist=["SelectionConfig"]).SelectionConfig()
    )
    rankings = all_rankings([c.candidate_id for c in candidates])
    client = FakeRankingClient(script=[{"rankings": rankings}])
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **kw: client)

    runner = CliRunner()
    result = runner.invoke(app, ["select", analysis.asset_id, "--local"])

    assert result.exit_code == 0, result.stdout
    reels_path = wd / "reels.json"
    assert reels_path.exists()
    data = json.loads(reels_path.read_text())
    assert data["asset_id"] == analysis.asset_id
    assert isinstance(data["reels"], list)
    assert len(data["reels"]) > 0


def test_cli_select_local_with_prompt(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reelforge_core.models import SelectionConfig

    analysis = make_analysis("b" * 64, [10.0] * 6)
    wd = isolated_data_dir / "working" / analysis.asset_id
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "analysis.json").write_text(analysis.model_dump_json(indent=2))

    candidates = generate_candidates(analysis, SelectionConfig())
    rankings = all_rankings([c.candidate_id for c in candidates], relevance=85)
    client = FakeRankingClient(script=[{"rankings": rankings}])
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **kw: client)

    runner = CliRunner()
    result = runner.invoke(
        app, ["select", analysis.asset_id, "--local", "--prompt", "clips of falls"]
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads((wd / "reels.json").read_text())
    assert data["config"]["prompt"] == "clips of falls"
    assert all(r["prompt_relevance"] == 85 for r in data["reels"])
