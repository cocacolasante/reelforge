"""Integration tests for select_reels with a mocked Anthropic client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.models import SelectionConfig
from reelforge_core.reels import generate_candidates, select_reels

from tests.reels._fake_ranking_client import (
    FakeRankingClient,
    all_rankings,
)
from tests.reels._fixtures import make_analysis


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, client) -> None:
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **kw: client)


async def test_happy_path_ranks_every_candidate(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid1", [10.0] * 8)
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)
    assert candidates, "expected some candidates for this fixture"

    client = FakeRankingClient(
        script=[{"rankings": all_rankings([c.candidate_id for c in candidates])}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)

    assert selection.candidates_generated == len(candidates)
    assert len(selection.reels) <= config.top_k
    assert all(r.rank >= 1 for r in selection.reels)
    # Sorted by rank and by overall desc
    overalls = [r.overall for r in selection.reels]
    assert overalls == sorted(overalls, reverse=True)
    # Titles are non-empty and distinct for this fake
    titles = {r.title for r in selection.reels}
    assert len(titles) == len(selection.reels)

    reels_path = isolated_data_dir / "working" / analysis.asset_id / "reels.json"
    assert reels_path.exists()
    data = json.loads(reels_path.read_text())
    assert data["asset_id"] == "aid1"


async def test_no_candidates_writes_empty_selection_without_api_call(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 5 scenes of 8s each → max 5 contiguous = 40s, but min window 6 scenes is impossible;
    # also no single window lands in [30, 60] because a single scene is 8s and 4 scenes = 32s? Wait, 4*8=32 yes.
    # To force zero candidates, make each scene 4s with max_scenes=6 → max 24s < 30s.
    analysis = make_analysis("aid2", [4.0] * 20)
    config = SelectionConfig(max_scenes_per_reel=6)

    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)

    assert selection.reels == []
    assert selection.candidates_generated == 0
    assert selection.anthropic_usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hits": 0,
    }
    assert client.calls == [], "should not call Anthropic when no candidates"


async def test_missing_candidate_triggers_retry_and_eventually_includes_it(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid3", [10.0] * 6)
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)
    assert len(candidates) >= 2
    missing_cid = candidates[0].candidate_id

    # First response omits one candidate; second response includes every id.
    first_partial = [
        r for r in all_rankings([c.candidate_id for c in candidates])
        if r["candidate_id"] != missing_cid
    ]
    second_full = all_rankings([c.candidate_id for c in candidates])
    client = FakeRankingClient(
        script=[{"rankings": first_partial}, {"rankings": second_full}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)

    # Two calls: initial + corrective retry.
    assert len(client.calls) == 2
    # The missing candidate either survived dedup (and is visible) or was dropped
    # by the greedy dedup. We only assert it made it through ranking by checking
    # ranking_raw.json, which captures the post-merge rankings before dedup.
    raw_path = isolated_data_dir / "working" / "aid3" / "ranking_raw.json"
    raw = json.loads(raw_path.read_text())
    ranked_ids = {r["candidate_id"] for r in raw["rankings"]}
    assert missing_cid in ranked_ids


async def test_extra_candidate_id_in_response_is_ignored(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid4", [10.0] * 4)
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)
    rankings = all_rankings([c.candidate_id for c in candidates])
    # Inject a bogus id not in the candidate set
    rankings.append(
        {
            "candidate_id": "ffffffffffffffff",
            "title": "ghost",
            "hook": "should be dropped",
            "justification": "not in candidate set",
            "suggested_mood": "neutral",
            "scores": {
                "narrative_coherence": 10,
                "hook_strength": 10,
                "emotional_payoff": 10,
                "standalone_clarity": 10,
            },
        }
    )
    client = FakeRankingClient(script=[{"rankings": rankings}])
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)
    for reel in selection.reels:
        assert reel.candidate_id != "ffffffffffffffff"


async def test_out_of_range_score_is_dropped(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid5", [10.0] * 4)
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)
    rankings = all_rankings([c.candidate_id for c in candidates])
    # Set one score out of range
    rankings[0]["scores"]["hook_strength"] = 150
    bad_cid = rankings[0]["candidate_id"]
    client = FakeRankingClient(script=[{"rankings": rankings}])
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)
    for reel in selection.reels:
        assert reel.candidate_id != bad_cid


async def test_silent_source_still_ranks(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid6", [10.0] * 5, with_audio=False)
    assert analysis.transcript is None
    assert analysis.loudness == []
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)
    assert candidates

    client = FakeRankingClient(
        script=[{"rankings": all_rankings([c.candidate_id for c in candidates])}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)
    assert len(selection.reels) > 0


async def test_resume_reuses_ranking_raw(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid7", [10.0] * 5)
    config_first = SelectionConfig(resume=False)
    candidates = generate_candidates(analysis, config_first)
    assert candidates

    client = FakeRankingClient(
        script=[{"rankings": all_rankings([c.candidate_id for c in candidates])}]
    )
    _patch_anthropic(monkeypatch, client)

    first = await select_reels(analysis, config_first)
    first_call_count = len(client.calls)

    # Second run with resume=True and unchanged config → should not hit Anthropic.
    config_second = SelectionConfig(resume=True)
    before = len(client.calls)
    second = await select_reels(analysis, config_second)
    assert len(client.calls) == before, "resume should skip the Claude call"
    assert second.candidates_generated == first.candidates_generated
    assert len(second.reels) == len(first.reels)


async def test_determinism_same_response_produces_identical_reels(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aid8", [10.0] * 6)
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)

    rankings = all_rankings([c.candidate_id for c in candidates])
    client = FakeRankingClient(script=[{"rankings": rankings}, {"rankings": rankings}])
    _patch_anthropic(monkeypatch, client)

    first = await select_reels(analysis, config)
    # Blow away reels.json so the second call writes a fresh one. Keep ranking_raw
    # so resume path isn't accidentally triggered (resume=False by default anyway).
    (isolated_data_dir / "working" / analysis.asset_id / "reels.json").unlink()
    second = await select_reels(analysis, config)

    def _diffable(sel):
        d = json.loads(sel.model_dump_json())
        d.pop("created_at", None)
        d.pop("elapsed_sec", None)
        return d

    assert _diffable(first) == _diffable(second)
