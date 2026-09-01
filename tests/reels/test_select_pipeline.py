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


def _shortlisted(analysis, config, candidates):
    """The candidates the pipeline will actually send to the ranker (CP5)."""
    from reelforge_core.reels.prescore import compute_features, shortlist

    return shortlist(candidates, compute_features(candidates, analysis), config.shortlist_size)


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
    # Ranks are sequential 1..n; note the ORDER follows the MMR diversity
    # re-rank (CP8), which may deliberately deviate from pure overall-desc.
    assert [r.rank for r in selection.reels] == list(range(1, len(selection.reels) + 1))
    # Titles are non-empty and distinct for this fake
    titles = {r.title for r in selection.reels}
    assert len(titles) == len(selection.reels)

    reels_path = isolated_data_dir / "working" / analysis.asset_id / "reels.json"
    assert reels_path.exists()
    data = json.loads(reels_path.read_text())
    assert data["asset_id"] == "aid1"
    # CP8 diversity stat is always present (0 when nothing was displaced).
    assert data["candidates_dropped_by_diversity"] >= 0


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
    short = _shortlisted(analysis, config, candidates)
    assert len(short) >= 2
    missing_cid = short[0].candidate_id

    # First response omits one shortlisted candidate; second includes every id.
    first_partial = [
        r for r in all_rankings([c.candidate_id for c in short])
        if r["candidate_id"] != missing_cid
    ]
    second_full = all_rankings([c.candidate_id for c in short])
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


# ---------------------------------------------------------------------------
# Natural-language prompt (Direction) feature
# ---------------------------------------------------------------------------


async def test_prompt_injects_direction_and_relevance_field(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aidp1", [10.0] * 6)
    config = SelectionConfig(prompt="clips of falls")
    candidates = generate_candidates(analysis, config)
    assert candidates

    client = FakeRankingClient(
        script=[{"rankings": all_rankings([c.candidate_id for c in candidates], relevance=80)}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)

    from reelforge_core.reels.rank import SYSTEM_PROMPT_V2

    system = client.calls[0]["system"]
    assert system.startswith(SYSTEM_PROMPT_V2)
    assert "USER DIRECTION" in system and "clips of falls" in system
    tool = client.calls[0]["tools"][0]
    item = tool["input_schema"]["properties"]["rankings"]["items"]
    assert "prompt_relevance" in item["properties"]
    assert "prompt_relevance" in item["required"]
    assert selection.reels, "80-relevance candidates must survive the gate"
    for r in selection.reels:
        assert r.prompt_relevance == 80
        assert r.overall == round(0.45 * 80 + 0.55 * r.scores.weighted, 2)


async def test_no_prompt_uses_v2_golden(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 golden (replaces the retired test_no_prompt_is_unchanged — v2 is a
    deliberate behavior change): listwise prompt, v2 tool schema, v2 stamp."""
    analysis = make_analysis("aidp2", [10.0] * 6)
    config = SelectionConfig()
    candidates = generate_candidates(analysis, config)

    client = FakeRankingClient(
        script=[{"rankings": all_rankings([c.candidate_id for c in candidates])}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)

    from reelforge_core.reels.rank import RECORD_RANKINGS_V2, SYSTEM_PROMPT_V2

    assert client.calls[0]["system"] == SYSTEM_PROMPT_V2
    assert client.calls[0]["tools"][0] == RECORD_RANKINGS_V2
    item = client.calls[0]["tools"][0]["input_schema"]["properties"]["rankings"]["items"]
    assert "prompt_relevance" not in item["properties"]
    assert {"rank_position", "opening_description", "content_style"} <= set(item["properties"])
    for req in ("rank_position", "opening_description", "content_style"):
        assert req in item["required"]
    assert client.calls[0]["max_tokens"] == 16000
    for r in selection.reels:
        assert r.prompt_relevance is None
        assert r.overall == round(r.scores.weighted, 2)
        assert r.rank_position is not None
        assert r.opening_description
        assert r.edit_style in ("classic", "hype", "talking_head", "cinematic", "chill")

    # Stamp golden: v2 prompt version + prescore version + shortlist hash.
    stamp = json.loads(
        (isolated_data_dir / "working" / "aidp2" / "ranking_raw.json.stamp").read_text()
    )
    assert stamp["ranking_prompt_version"] == "v3"
    assert stamp["prescore_version"] == "p1"
    assert set(stamp) == {
        "ranking_model",
        "ranking_prompt_version",
        "temperature",
        "candidate_hash",
        "prescore_version",
    }


async def test_relevance_floor_gates_before_dedup(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aidp3", [10.0] * 6)
    config = SelectionConfig(prompt="jumps")
    candidates = generate_candidates(analysis, config)
    short = _shortlisted(analysis, config, candidates)
    assert len(short) >= 2

    ids = [c.candidate_id for c in short]
    relevance = {cid: (90 if i == 0 else 10) for i, cid in enumerate(ids)}
    client = FakeRankingClient(script=[{"rankings": all_rankings(ids, relevance=relevance)}])
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)

    kept_ids = {r.candidate_id for r in selection.reels}
    assert kept_ids == {ids[0]}, "below-floor candidates must be filtered out"
    # Gate drops are NOT counted as dedup drops.
    assert selection.candidates_dropped_by_dedup == 0


async def test_prompt_all_below_floor_writes_empty_reels(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aidp4", [10.0] * 6)
    config = SelectionConfig(prompt="clips of unicorns")
    candidates = generate_candidates(analysis, config)

    client = FakeRankingClient(
        script=[{"rankings": all_rankings([c.candidate_id for c in candidates], relevance=5)}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)
    assert selection.reels == []
    reels_path = isolated_data_dir / "working" / analysis.asset_id / "reels.json"
    assert json.loads(reels_path.read_text())["reels"] == []


async def test_resume_invalidated_by_prompt_change(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("aidp5", [10.0] * 5)
    config_first = SelectionConfig(prompt="falls")
    candidates = generate_candidates(analysis, config_first)
    ids = [c.candidate_id for c in candidates]

    client = FakeRankingClient(script=[{"rankings": all_rankings(ids, relevance=70)}])
    _patch_anthropic(monkeypatch, client)

    await select_reels(analysis, config_first)
    calls_after_first = len(client.calls)

    # Same prompt + resume → cache hit, no new call, relevance survives re-coercion.
    second = await select_reels(analysis, SelectionConfig(resume=True, prompt="falls"))
    assert len(client.calls) == calls_after_first
    assert all(r.prompt_relevance == 70 for r in second.reels)

    # Different prompt + resume → fresh ranking call.
    await select_reels(analysis, SelectionConfig(resume=True, prompt="jumps"))
    assert len(client.calls) == calls_after_first + 1

    # Prompt removed + resume → fresh call again (stamp mismatch).
    await select_reels(analysis, SelectionConfig(resume=True))
    assert len(client.calls) == calls_after_first + 2


async def test_missing_prompt_relevance_drops_entry_and_triggers_retry(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 8 scenes so the shortlist's overlap walk keeps >= 2 spans (with 5 scenes
    # every candidate nests inside 0-50 and only one survives).
    analysis = make_analysis("aidp6", [10.0] * 8)
    config = SelectionConfig(prompt="falls")
    candidates = generate_candidates(analysis, config)
    ids = [c.candidate_id for c in _shortlisted(analysis, config, candidates)]
    assert len(ids) >= 2

    # First response: one entry lacks prompt_relevance → dropped → corrective
    # retry; second response is complete.
    first = all_rankings(ids, relevance=60)
    del first[0]["prompt_relevance"]
    client = FakeRankingClient(
        script=[{"rankings": first}, {"rankings": all_rankings(ids, relevance=60)}]
    )
    _patch_anthropic(monkeypatch, client)

    selection = await select_reels(analysis, config)
    assert len(client.calls) == 2, "missing relevance must trigger the corrective retry"
    assert selection.reels, "retry must recover a usable ranking"
    # The retried candidate lands in the merged raw rankings (dedup may still
    # collapse it out of the final list — these spans overlap heavily).
    raw = json.loads(
        (isolated_data_dir / "working" / analysis.asset_id / "ranking_raw.json").read_text()
    )
    retried = [r for r in raw["rankings"] if r["candidate_id"] == ids[0]]
    assert retried and "prompt_relevance" in retried[-1]
