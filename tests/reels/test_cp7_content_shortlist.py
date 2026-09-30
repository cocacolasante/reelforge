"""Pro-editing CP7: content-scored shortlist slots, the rank-position blend,
and transcript overlap in the diversity step."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reelforge_core.models import ReelCandidate
from reelforge_core.reels.content_score import candidate_scores, parse_scores, score_units
from reelforge_core.reels.dedup import content_words, mmr_diversify, similarity
from reelforge_core.reels.generators.sentence import UtteranceUnit
from reelforge_core.reels.prescore import PrescoreFeatures, shortlist
from reelforge_core.reels.rank import BORDA_WEIGHT, blend_rank_positions
from tests.compose.test_speech_snap import _analysis, _reel, _scene
from reelforge_core.models import TranscriptWord


def _cand(cid: str, start: float, end: float) -> ReelCandidate:
    return ReelCandidate(candidate_id=cid, scene_indices=[0], start_sec=start, end_sec=end,
                         duration_sec=end - start, scene_count=1)


def _features(score_by_id: dict[str, float]) -> dict[str, PrescoreFeatures]:
    # n_scene_cuts drives prescore linearly (+5 each, capped at 4) — enough to
    # order a test set.
    out = {}
    for cid, cuts in score_by_id.items():
        out[cid] = PrescoreFeatures(
            starts_on_unit_boundary=False, ends_on_unit_boundary=False,
            starts_mid_word=False, ends_mid_word=False, speech_ratio=0.0,
            energy_peak_z=None, energy_peak_pos=None, lufs_range=0.0,
            n_scene_cuts=int(cuts), source="sentence",
        )
    return out


# ---- line scores --------------------------------------------------------------------


def test_parse_scores_keeps_only_valid_entries():
    raw = {"scores": [
        {"line": 0, "hook": 9, "payoff": 2},
        {"line": 0, "hook": 1, "payoff": 1},  # duplicate: first wins
        {"line": 1, "hook": 11, "payoff": 3},  # out of range
        {"line": 5, "hook": 3, "payoff": 3},  # no such line
        {"line": 2, "hook": "x", "payoff": 3},
        {"line": 3, "hook": 4, "payoff": 8},
    ]}
    assert parse_scores(raw, 4) == {0: (9, 2), 3: (4, 8)}


def test_candidate_score_is_opening_hook_plus_closing_payoff():
    units = [UtteranceUnit(0, 3, "a", 2), UtteranceUnit(3, 6, "b", 2), UtteranceUnit(6, 9, "c", 2)]
    scores = {0: (9, 1), 1: (2, 2), 2: (1, 7)}
    got = candidate_scores([_cand("x", 0.0, 9.0), _cand("y", 3.0, 6.0), _cand("z", 20.0, 30.0)],
                           units, scores)
    assert got == {"x": 16.0, "y": 4.0}  # z has no speech: no score


@pytest.mark.asyncio
async def test_score_units_is_stamped_and_failure_is_empty(tmp_path):
    units = [UtteranceUnit(0, 2, "the result doubled", 1.5), UtteranceUnit(2, 4, "so yeah", 1)]

    class Client:
        calls = 0
        messages = None

        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            Client.calls += 1
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input={"scores": [
                    {"line": 0, "hook": 8, "payoff": 7}, {"line": 1, "hook": 0, "payoff": 1}]})],
                stop_reason="tool_use", usage=SimpleNamespace(input_tokens=50, output_tokens=10))

    got, usage = await score_units(units, working_dir=tmp_path, client=Client())
    assert got == {0: (8, 7), 1: (0, 1)} and usage.input_tokens == 50
    again, usage2 = await score_units(units, working_dir=tmp_path, client=Client())
    assert again == got and Client.calls == 1 and usage2.input_tokens == 0
    # Default client is blocked in tests; a new unit set misses the stamp.
    none, _ = await score_units(units[:1], working_dir=tmp_path)
    assert none == {}


# ---- shortlist ------------------------------------------------------------------------


def test_reserved_slots_go_to_strong_lines_the_heuristic_missed():
    cands = [_cand(f"c{i}", i * 100.0, i * 100.0 + 30.0) for i in range(10)]
    feats = _features({f"c{i}": 4 - min(i, 4) for i in range(10)})  # c0 best ... c4+ tied low
    plain = shortlist(cands, feats, 4)
    assert [c.candidate_id for c in plain] == ["c0", "c1", "c2", "c3"]
    content = {"c9": 18.0, "c8": 12.0, "c1": 20.0, "c7": 5.0}
    got = shortlist(cands, feats, 4, content=content, reserved=2)
    ids = [c.candidate_id for c in got]
    # Heuristic keeps its top 2; the reserved 2 go to c9 and c8 (c1 already
    # in, c7 under the content floor). Returned in prescore order.
    assert set(ids) == {"c0", "c1", "c9", "c8"} and ids[:2] == ["c0", "c1"]


def test_reserved_slots_fall_back_to_the_heuristic():
    cands = [_cand(f"c{i}", i * 100.0, i * 100.0 + 30.0) for i in range(6)]
    feats = _features({f"c{i}": 4 - min(i, 4) for i in range(6)})
    got = shortlist(cands, feats, 4, content={"c5": 1.0}, reserved=3)
    assert [c.candidate_id for c in got] == ["c0", "c1", "c2", "c3"]


def test_reserved_slots_respect_the_overlap_rule():
    cands = [_cand("a", 0, 30), _cand("b", 100, 130), _cand("a2", 1, 31), _cand("c", 200, 230)]
    feats = _features({"a": 4, "b": 3, "a2": 0, "c": 0})
    got = shortlist(cands, feats, 3, content={"a2": 20.0, "c": 10.0}, reserved=1)
    assert [c.candidate_id for c in got] == ["a", "b", "c"]  # a2 duplicates a


# ---- rank-position blend ----------------------------------------------------------------


def test_blend_rank_positions():
    reels = [_reel([0], 0, 30).model_copy(update={"candidate_id": cid, "overall": 70.0,
                                                  "rank_position": pos})
             for cid, pos in (("a", 1), ("b", 2), ("c", 3), ("d", None))]
    out = {r.candidate_id: r.overall for r in blend_rank_positions(reels)}
    assert out == {"a": 70.0 + BORDA_WEIGHT / 2, "b": 70.0, "c": 70.0 - BORDA_WEIGHT / 2, "d": 70.0}
    single = reels[:1]
    assert blend_rank_positions(single) == single


# ---- diversity --------------------------------------------------------------------------


def test_content_words_and_text_similarity():
    words = [TranscriptWord(start=i * 0.4, end=i * 0.4 + 0.3, word=f" {w}", probability=1)
             for i, w in enumerate("the wax scraper removes wax from your skimboard fast".split())]
    a = _analysis([_scene(0, 0, 10)], words)
    got = content_words(a.transcript, 0.0, 10.0)
    # "wax" is too short, "the"/"your" are stop words.
    assert got == {"scraper", "removes", "from", "skimboard", "fast"}
    assert similarity(set(), set(), "calm", "joyful", {"x", "y"}, {"x", "y"}) == pytest.approx(0.5)
    assert similarity(set(), set(), "calm", "joyful") == 0.0


def test_mmr_uses_what_the_reels_say():
    r1 = _reel([0], 0, 30).model_copy(update={"candidate_id": "r1", "overall": 80.0, "suggested_mood": "calm"})
    r2 = _reel([0], 40, 70).model_copy(update={"candidate_id": "r2", "overall": 78.0, "suggested_mood": "joyful"})
    r3 = _reel([0], 80, 110).model_copy(update={"candidate_id": "r3", "overall": 76.0, "suggested_mood": "tense"})
    tags = {"r1": set(), "r2": set(), "r3": set()}
    words = {"r1": {"wax", "scraper"}, "r2": {"wax", "scraper"}, "r3": {"surf"}}
    plain = [r.candidate_id for r in mmr_diversify([r1, r2, r3], tags, 8.0)]
    aware = [r.candidate_id for r in mmr_diversify([r1, r2, r3], tags, 8.0, words)]
    assert plain == ["r1", "r2", "r3"] and aware == ["r1", "r3", "r2"]
