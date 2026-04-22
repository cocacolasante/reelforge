"""rank.py: retry policy, malformed response, batched path, extract helpers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from reelforge_core.errors import RankingError
from reelforge_core.reels import generate_candidates, rank as rank_mod
from reelforge_core.reels.rank import (
    _extract_rankings,
    _is_retryable,
    build_candidate_context,
)

from tests.reels._fake_ranking_client import FakeRankingClient, all_rankings
from tests.reels._fixtures import make_analysis


@dataclass
class _ToolBlock:
    type: str
    input: dict | str


def test_extract_rankings_handles_dict_input() -> None:
    resp = SimpleNamespace(
        content=[_ToolBlock(type="tool_use", input={"rankings": [{"x": 1}]})],
        stop_reason="tool_use",
    )
    assert _extract_rankings(resp) == [{"x": 1}]


def test_extract_rankings_handles_string_json_input() -> None:
    resp = SimpleNamespace(
        content=[_ToolBlock(type="tool_use", input='{"rankings": [{"y": 2}]}')],
        stop_reason="tool_use",
    )
    assert _extract_rankings(resp) == [{"y": 2}]


def test_extract_rankings_raises_when_no_tool_use() -> None:
    resp = SimpleNamespace(content=[_ToolBlock(type="text", input="nope")], stop_reason="end_turn")
    with pytest.raises(RankingError):
        _extract_rankings(resp)


def test_extract_rankings_raises_on_bad_shape() -> None:
    resp = SimpleNamespace(
        content=[_ToolBlock(type="tool_use", input={"other": "shape"})],
        stop_reason="tool_use",
    )
    with pytest.raises(RankingError):
        _extract_rankings(resp)


def test_is_retryable_classification() -> None:
    import anthropic

    # Real exceptions that should retry
    try:
        raise anthropic.APIConnectionError(request=None)  # type: ignore[call-arg]
    except anthropic.APIConnectionError as e:
        assert _is_retryable(e) is True

    # Random value errors don't
    assert _is_retryable(ValueError("x")) is False


class _RaisingClient:
    """Fake client whose `.messages.create` raises a non-retryable error once."""

    def __init__(self, to_raise: Exception) -> None:
        self._exc = to_raise
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_rank_raises_on_non_retryable_error() -> None:
    analysis = make_analysis("aid-r1", [10.0] * 6)
    from reelforge_core.models import SelectionConfig

    cfg = SelectionConfig()
    candidates = generate_candidates(analysis, cfg)
    client = _RaisingClient(ValueError("fatal"))
    with pytest.raises(RankingError):
        await rank_mod.rank(candidates, analysis, cfg, client=client)
    assert client.calls == 1  # no retries on non-retryable


class _MissingThenFullClient:
    """First call omits one candidate; second call is comprehensive."""

    def __init__(self, candidate_ids: list[str], drop_first: str) -> None:
        self.ids = candidate_ids
        self.drop = drop_first
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            rankings = [r for r in all_rankings(self.ids) if r["candidate_id"] != self.drop]
        else:
            rankings = all_rankings(self.ids)
        return SimpleNamespace(
            content=[_ToolBlock(type="tool_use", input={"rankings": rankings})],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


@pytest.mark.asyncio
async def test_rank_missing_candidate_retry_merges() -> None:
    analysis = make_analysis("aid-r2", [10.0] * 6)
    from reelforge_core.models import SelectionConfig

    cfg = SelectionConfig()
    cands = generate_candidates(analysis, cfg)
    assert cands
    client = _MissingThenFullClient([c.candidate_id for c in cands], cands[0].candidate_id)
    result = await rank_mod.rank(cands, analysis, cfg, client=client)
    assert client.calls == 2  # initial + corrective
    got_ids = {r.candidate_id for r in result.reels}
    # Every original candidate survived after the merge
    assert got_ids == {c.candidate_id for c in cands}


def test_build_candidate_context_populates_fields() -> None:
    analysis = make_analysis("ctx", [10.0] * 5)
    from reelforge_core.models import SelectionConfig

    cfg = SelectionConfig()
    candidates = generate_candidates(analysis, cfg)
    assert candidates
    ctx = build_candidate_context(candidates[0], analysis)
    assert ctx["candidate_id"] == candidates[0].candidate_id
    assert ctx["scene_count"] >= 1
    assert "scenes" in ctx and ctx["scenes"]
    assert "transcript" in ctx
    # Loudness stats either None (no samples in span) or a dict with the keys.
    ls = ctx["loudness_stats"]
    assert set(ls.keys()) == {"mean_lufs", "peak_lufs", "dynamic_range_lu"}


def test_build_candidate_context_long_transcript_truncates() -> None:
    from reelforge_core.models import (
        SelectionConfig,
        Transcript,
        TranscriptSegment,
        TranscriptWord,
    )

    analysis = make_analysis("ctx-long", [10.0] * 5)
    # Inject a huge transcript segment overlapping the first candidate span
    huge_text = "word " * 5000
    analysis = analysis.model_copy(
        update={
            "transcript": Transcript(
                language="en",
                language_probability=1.0,
                duration=50.0,
                segments=[
                    TranscriptSegment(
                        start=0.0,
                        end=50.0,
                        text=huge_text,
                        words=[
                            TranscriptWord(
                                start=0.0, end=0.1, word="word", probability=0.9
                            )
                        ],
                    )
                ],
            )
        }
    )
    cfg = SelectionConfig()
    candidates = generate_candidates(analysis, cfg)
    ctx = build_candidate_context(candidates[0], analysis)
    # Truncation marker present, length ≤ cap + tolerance
    assert "truncated" in ctx["transcript"]
    from reelforge_core.reels.rank import TRANSCRIPT_SLICE_CAP

    assert len(ctx["transcript"]) <= TRANSCRIPT_SLICE_CAP + 20


def test_build_candidate_context_no_transcript_is_empty_string() -> None:
    analysis = make_analysis("ctx-nt", [10.0] * 5, with_audio=False)
    from reelforge_core.models import SelectionConfig

    cfg = SelectionConfig()
    candidates = generate_candidates(analysis, cfg)
    ctx = build_candidate_context(candidates[0], analysis)
    assert ctx["transcript"] == ""


def test_coerce_rankings_drops_duplicate_candidate_id() -> None:
    from reelforge_core.reels.rank import _coerce_rankings

    analysis = make_analysis("dup", [10.0] * 5)
    from reelforge_core.models import SelectionConfig

    cfg = SelectionConfig()
    cands = generate_candidates(analysis, cfg)
    assert cands
    rankings = all_rankings([cands[0].candidate_id])
    # Duplicate the same candidate entry
    rankings = rankings + [rankings[0]]
    cand_map = {c.candidate_id: c for c in cands}
    out = _coerce_rankings(rankings, candidate_map=cand_map)
    # Second (duplicate) entry ignored
    assert len(out) == 1


def test_coerce_rankings_drops_out_of_range_score() -> None:
    from reelforge_core.reels.rank import _coerce_rankings

    analysis = make_analysis("bad-score", [10.0] * 4)
    from reelforge_core.models import SelectionConfig

    cfg = SelectionConfig()
    cands = generate_candidates(analysis, cfg)
    rankings = all_rankings([cands[0].candidate_id])
    rankings[0]["scores"]["hook_strength"] = 500
    cand_map = {c.candidate_id: c for c in cands}
    out = _coerce_rankings(rankings, candidate_map=cand_map)
    assert out == []
