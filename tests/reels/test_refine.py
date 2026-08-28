"""CP7: boundary refinement — pure validation rules + pipeline wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.models import (
    SelectionConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from reelforge_core.reels import select_reels
from reelforge_core.reels.refine import (
    REFINE_WINDOW_SEC,
    apply_refinement,
    build_refinement_context,
)

from tests.reels._fake_ranking_client import FakeRankingClient
from tests.reels._fixtures import make_analysis


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, client) -> None:
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **kw: client)


def _reel_from(analysis, start: float, end: float):
    from reelforge_core.models import RankedReel, ReelScores

    return RankedReel(
        candidate_id="r1",
        scene_indices=[0],
        start_sec=start,
        end_sec=end,
        duration_sec=end - start,
        title="t",
        hook="h",
        justification="j",
        scores=ReelScores(
            narrative_coherence=70, hook_strength=70, emotional_payoff=70, standalone_clarity=70
        ),
        overall=70.0,
        rank=1,
        suggested_mood="neutral",
    )


def _with_words(analysis, words: list[TranscriptWord]):
    transcript = Transcript(
        language="en",
        language_probability=1.0,
        duration=analysis.duration,
        segments=[
            TranscriptSegment(
                start=words[0].start, end=words[-1].end, text="x", words=words
            )
        ],
    )
    return analysis.model_copy(update={"transcript": transcript})


# ---- apply_refinement rules ------------------------------------------------


def test_apply_refinement_clamps_to_window():
    analysis = make_analysis("rf1", [100.0], with_audio=False)
    reel = _reel_from(analysis, 30.0, 70.0)
    refined = apply_refinement(reel, 10.0, 90.0, analysis, SelectionConfig())
    # ±6s window: start clamps to 24, end to 76 (duration 52, within [30, 60]).
    assert refined.start_sec == 24.0
    assert refined.end_sec == 76.0
    assert refined.pre_refine_start_sec == 30.0
    assert refined.pre_refine_end_sec == 70.0
    assert refined.duration_sec == pytest.approx(52.0)


def test_apply_refinement_rejects_duration_violation():
    analysis = make_analysis("rf2", [100.0], with_audio=False)
    reel = _reel_from(analysis, 0.0, 30.0)
    # 5 → 29 would be 24s < 30s minimum: reject wholesale.
    refined = apply_refinement(reel, 5.0, 29.0, analysis, SelectionConfig())
    assert refined.start_sec == 0.0 and refined.end_sec == 30.0
    assert refined.pre_refine_start_sec is None


def test_apply_refinement_snaps_mid_word_edge():
    analysis = make_analysis("rf3", [100.0])
    analysis = _with_words(
        analysis,
        [TranscriptWord(start=24.5, end=25.2, word="hey", probability=0.9)],
    )
    reel = _reel_from(analysis, 30.0, 70.0)
    refined = apply_refinement(reel, 24.8, 70.0, analysis, SelectionConfig())
    # 24.8 is inside (24.5, 25.2), 0.3s in -> snapped back to the word start.
    assert refined.start_sec == 24.5
    assert refined.pre_refine_start_sec == 30.0


def test_apply_refinement_reverts_unsnappable_edge():
    analysis = make_analysis("rf4", [100.0])
    # Overlapping words: the snap retreats out of word 1 straight into word 2.
    analysis = _with_words(
        analysis,
        [
            TranscriptWord(start=24.0, end=25.5, word="looong", probability=0.9),
            TranscriptWord(start=25.3, end=26.5, word="overlap", probability=0.9),
        ],
    )
    reel = _reel_from(analysis, 30.0, 70.0)
    # 24.7 is 0.7s into word 1 (> 0.6 nudge) -> retreat to 25.5 -> inside
    # word 2 -> revert the start edge entirely.
    refined = apply_refinement(reel, 24.7, 74.0, analysis, SelectionConfig())
    assert refined.start_sec == 30.0
    assert refined.end_sec == 74.0  # the clean end edge still applied


def test_apply_refinement_recomputes_covering_scenes():
    analysis = make_analysis("rf5", [40.0, 40.0], with_audio=False)
    reel = _reel_from(analysis, 35.0, 70.0)
    reel = reel.model_copy(update={"scene_indices": [0, 1]})
    refined = apply_refinement(reel, 40.0, 70.0, analysis, SelectionConfig())
    # New span (40, 70) sits entirely in scene 1.
    assert refined.scene_indices == [1]
    assert refined.start_sec == 40.0 and refined.end_sec == 70.0


def test_apply_refinement_unchanged_bounds_leave_no_pre_refine():
    analysis = make_analysis("rf6", [100.0], with_audio=False)
    reel = _reel_from(analysis, 30.0, 70.0)
    refined = apply_refinement(reel, 30.0, 70.0, analysis, SelectionConfig())
    assert refined is reel


def test_build_refinement_context_shape():
    analysis = make_analysis("rf7", [40.0, 40.0])
    from reelforge_core.reels.generators.sentence import build_units

    units = build_units(analysis.transcript)
    reel = _reel_from(analysis, 20.0, 55.0)
    ctx = build_refinement_context(reel, analysis, units)
    assert ctx["candidate_id"] == "r1"
    assert ctx["start_edge_window"] == [20.0 - REFINE_WINDOW_SEC, 20.0 + REFINE_WINDOW_SEC]
    assert {"words_near_start", "words_near_end", "unit_boundary_starts", "energy_series"} <= set(ctx)


# ---- pipeline wiring -------------------------------------------------------


def _safe_shift(top, asset_duration: float, delta: float):
    """A refinement move that always survives the window/duration rules for
    the [10]*8 fixture (words sit at scene starts; shifted edges avoid them)."""
    if top.duration_sec - delta >= 30.0:
        return top.start_sec, top.end_sec - delta
    if top.end_sec + delta <= asset_duration:
        return top.start_sec, top.end_sec + delta
    return top.start_sec - delta, top.end_sec


async def test_pipeline_applies_refinement_and_persists_raw(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("rfp1", [10.0] * 8)
    config = SelectionConfig()

    # Pass 1: discover the final rank-1 reel (no-op refinement).
    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)
    first = await select_reels(analysis, config)
    assert first.reels
    top = first.reels[0]

    # Pass 2: shift one edge by 4s (direction chosen to stay in-bounds).
    new_start, new_end = _safe_shift(top, 80.0, 4.0)
    refine_script = [
        {
            "refinements": [
                {
                    "candidate_id": top.candidate_id,
                    "new_start_sec": new_start,
                    "new_end_sec": new_end,
                    "reason": "carry the payoff",
                }
            ]
        }
    ]
    client2 = FakeRankingClient(script=[], refine_script=refine_script)
    _patch_anthropic(monkeypatch, client2)
    second = await select_reels(analysis, config)
    refined_top = next(r for r in second.reels if r.candidate_id == top.candidate_id)
    assert (refined_top.start_sec, refined_top.end_sec) == (
        pytest.approx(new_start),
        pytest.approx(new_end),
    )
    assert refined_top.pre_refine_start_sec == pytest.approx(top.start_sec)
    assert refined_top.pre_refine_end_sec == pytest.approx(top.end_sec)
    assert len(client2.refine_calls) == 1

    wd = isolated_data_dir / "working" / "rfp1"
    raw = json.loads((wd / "refine_raw.json").read_text())
    assert raw["refinements"][0]["candidate_id"] == top.candidate_id
    assert (wd / "refine_raw.json.stamp").exists()
    # Usage sums ranking + refinement (fake: 100/200 rank + 40/30 refine).
    assert second.anthropic_usage["input_tokens"] == 140
    assert second.anthropic_usage["output_tokens"] == 230


async def test_pipeline_refinement_failure_keeps_bounds(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("rfp2", [10.0] * 8)
    client = FakeRankingClient(script=[], refine_script=[{"garbage": True}])
    _patch_anthropic(monkeypatch, client)
    selection = await select_reels(analysis, SelectionConfig())
    assert selection.reels
    for r in selection.reels:
        assert r.pre_refine_start_sec is None
    wd = isolated_data_dir / "working" / "rfp2"
    assert (wd / "reels.json").exists()
    assert not (wd / "refine_raw.json").exists()  # failures are not stamped


async def test_pipeline_refine_disabled_makes_no_call(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("rfp3", [10.0] * 8)
    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)
    await select_reels(analysis, SelectionConfig(refine=False))
    assert client.refine_calls == []


async def test_pipeline_refine_resume_cache_hit(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = make_analysis("rfp4", [10.0] * 8)

    client = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client)
    first = await select_reels(analysis, SelectionConfig(resume=True))
    top = first.reels[0]
    # Seed a real (persisted) refinement so the stamp exists.
    new_start, new_end = _safe_shift(top, 80.0, 3.0)
    refine_script = [
        {
            "refinements": [
                {
                    "candidate_id": top.candidate_id,
                    "new_start_sec": new_start,
                    "new_end_sec": new_end,
                    "reason": "seed",
                }
            ]
        }
    ]
    client2 = FakeRankingClient(script=[], refine_script=refine_script)
    _patch_anthropic(monkeypatch, client2)
    second = await select_reels(analysis, SelectionConfig(resume=True))
    assert len(client2.refine_calls) == 1

    # Third run: ranking AND refinement both cache-hit; bounds replayed.
    client3 = FakeRankingClient(script=[])
    _patch_anthropic(monkeypatch, client3)
    third = await select_reels(analysis, SelectionConfig(resume=True))
    assert client3.calls == [] and client3.refine_calls == []
    refined_top = next(r for r in third.reels if r.candidate_id == top.candidate_id)
    assert (refined_top.start_sec, refined_top.end_sec) == (
        pytest.approx(new_start),
        pytest.approx(new_end),
    )
    assert third.anthropic_usage["input_tokens"] == 0
