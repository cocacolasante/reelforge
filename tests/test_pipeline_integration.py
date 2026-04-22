"""Integration tests for the analysis pipeline. Docker not required, but ffmpeg is."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core import probe
from reelforge_core.analysis import analyze
from reelforge_core.models import AnalysisConfig


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, client) -> None:
    """Replace AsyncAnthropic so analyze_semantics constructs our fake."""
    import anthropic  # type: ignore

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **kw: client)


async def test_pipeline_multiscene(
    multiscene_mp4: Path,
    isolated_data_dir: Path,
    fake_anthropic,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_anthropic(monkeypatch, fake_anthropic)

    asset = probe(multiscene_mp4)
    config = AnalysisConfig(whisper_model="base.en", scene_threshold=27.0)
    report = await analyze(asset, config)

    # Scenes
    assert len(report.scenes) >= 2, f"expected >=2 scenes, got {len(report.scenes)}"
    for s in report.scenes:
        thumb = isolated_data_dir / "working" / asset.id / s.thumbnail_path
        assert thumb.exists(), f"thumbnail missing for scene {s.index}: {thumb}"
        assert thumb.stat().st_size > 0

    # Loudness
    assert len(report.loudness) > 0
    for p in report.loudness:
        assert p.lufs >= -80.0

    # Semantics: one per scene, moods/tags not all identical
    assert len(report.semantics) == len(report.scenes)
    moods = {s.mood for s in report.semantics}
    # Fake responder varies by scene index, so at least some variance expected.
    if len(report.scenes) > 1:
        assert len(moods) > 1, "semantics moods should not all be identical"

    # analysis.json on disk and loadable
    analysis_path = isolated_data_dir / "working" / asset.id / "analysis.json"
    assert analysis_path.exists()
    data = json.loads(analysis_path.read_text())
    assert data["asset_id"] == asset.id
    assert data["anthropic_usage"]["cache_hits"] == 0
    assert data["anthropic_usage"]["input_tokens"] > 0


async def test_pipeline_silent_video(
    silent_mp4: Path,
    isolated_data_dir: Path,
    fake_anthropic,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda *a, **kw: fake_anthropic)

    asset = probe(silent_mp4)
    config = AnalysisConfig(whisper_model="base.en")
    report = await analyze(asset, config)

    assert report.has_audio is False
    assert report.transcript is None
    assert report.loudness == []
    for s in report.semantics:
        # Fake responder sets has_speech based on scene index parity; we only
        # care that the pipeline tolerates a silent source without errors.
        assert isinstance(s.has_speech, bool)


async def test_pipeline_resume_uses_cache(
    multiscene_mp4: Path,
    isolated_data_dir: Path,
    fake_anthropic,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda *a, **kw: fake_anthropic)

    asset = probe(multiscene_mp4)
    config1 = AnalysisConfig(whisper_model="base.en", scene_threshold=27.0, resume=False)
    first_report = await analyze(asset, config1)
    first_call_count = len(fake_anthropic.calls)
    assert first_call_count == len(first_report.scenes)

    # Snapshot mtimes of intermediate artifacts.
    wd = isolated_data_dir / "working" / asset.id
    snapshots = {
        "scenes.json": (wd / "scenes.json").stat().st_mtime,
        "transcript.json": (wd / "transcript.json").stat().st_mtime,
        "loudness.json": (wd / "loudness.json").stat().st_mtime,
    }

    # Delete semantics.json and re-run with --resume — scenes/transcript/loudness
    # must not be recomputed; every semantics call should hit the SQLite cache.
    (wd / "semantics.json").unlink()
    (wd / "analysis.json").unlink()

    config2 = AnalysisConfig(whisper_model="base.en", scene_threshold=27.0, resume=True)
    before_calls = len(fake_anthropic.calls)
    second_report = await analyze(asset, config2)
    assert len(fake_anthropic.calls) == before_calls, (
        "semantics should have been fully cache-hit on resume"
    )
    assert all(s.cached for s in second_report.semantics)

    for name, mtime in snapshots.items():
        assert (wd / name).stat().st_mtime == mtime, (
            f"{name} should not have been rewritten on resume"
        )
