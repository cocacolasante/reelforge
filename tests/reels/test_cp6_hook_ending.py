"""Pro-editing CP6: hook features, endings that land, trailing-filler trims
and cold opens — every model proposal validated locally."""

from __future__ import annotations

import pytest

from reelforge_core.compose.director import apply_director
from reelforge_core.compose.sfx import cold_open_exit, plan_sfx
from reelforge_core.compose.styles import (
    COLD_OPEN_PAYOFF_MIN,
    EditPlan,
    PlannedShot,
    cold_open_for,
    lock_cold_open,
    plan_edit,
    with_cold_open,
)
from reelforge_core.models import (
    ComposeConfig,
    ReelCandidate,
    ReelScores,
    SelectionConfig,
    TranscriptWord,
)
from reelforge_core.reels.rank import _coerce_rankings, hook_features, validate_cold_open
from reelforge_core.reels.refine import trim_trailing
from tests.compose.test_speech_snap import _analysis, _reel, _scene


def _speech(text: str, t0: float = 0.5, step: float = 0.4) -> list[TranscriptWord]:
    out = []
    for i, w in enumerate(text.split()):
        s = round(t0 + i * step, 3)
        out.append(TranscriptWord(start=s, end=round(s + 0.3, 3), word=" " + w, probability=0.9))
    return out


TALK = (
    "hey guys welcome back. today I will show you the one trick that fixed my "
    "board forever and it only takes a minute. the result was twice as fast. so yeah"
)


def _talk_analysis():
    return _analysis([_scene(0, 0.0, 40.0)], _speech(TALK))


# ---- hook features --------------------------------------------------------------


def test_hook_features_measure_opening_and_ending():
    a = _talk_analysis()
    f = hook_features(a, 0.0, 40.0)
    assert f["first_word_sec"] == 0.5
    assert f["opening_greeting"].lower().startswith("hey guys")
    assert f["trailing_filler"] == "so yeah"
    assert f["ends_on_sentence"] is False
    assert hook_features(_analysis([_scene(0, 0.0, 10.0)], None), 0.0, 10.0) == {"has_speech": False}


# ---- cold open validation ---------------------------------------------------------


def test_valid_cold_open_is_kept():
    a = _talk_analysis()
    # "the result was twice as fast." sits at words 23-28: 9.7s-12.0s.
    got = validate_cold_open({"start_sec": 9.65, "end_sec": 12.05}, 0.0, 40.0, a)
    assert got == (9.65, 12.05)


@pytest.mark.parametrize(
    "raw",
    [
        {"start_sec": 2.0, "end_sec": 4.0},  # inside the opening 5s
        {"start_sec": 10.0, "end_sec": 16.0},  # too long
        {"start_sec": 38.5, "end_sec": 41.0},  # past the span
        {"start_sec": 12.0, "end_sec": 12.5},  # too short
        None,
        {"start_sec": "x"},
    ],
)
def test_bad_cold_opens_are_dropped(raw):
    assert validate_cold_open(raw, 0.0, 40.0, _talk_analysis()) is None


def test_cold_open_edges_snap_off_words():
    a = _talk_analysis()
    # 9.8 is inside "result" (9.7-10.0): snaps back to its start.
    got = validate_cold_open({"start_sec": 9.8, "end_sec": 12.0}, 0.0, 40.0, a)
    assert got is not None and got[0] == pytest.approx(9.7)


def _candidate(start=0.0, end=40.0) -> ReelCandidate:
    return ReelCandidate(candidate_id="c1", scene_indices=[0], start_sec=start, end_sec=end,
                         duration_sec=end - start, scene_count=1)


def _entry(**extra) -> dict:
    return {
        "candidate_id": "c1", "title": "T", "hook": "H", "justification": "J",
        "suggested_mood": "calm",
        "scores": {"narrative_coherence": 60, "hook_strength": 60,
                   "emotional_payoff": 60, "standalone_clarity": 60},
        **extra,
    }


def test_coerce_carries_ending_cold_open_and_tail():
    a = _talk_analysis()
    (base,) = _coerce_rankings([_entry()], candidate_map={"c1": _candidate()})
    (reel,) = _coerce_rankings(
        [_entry(ending_lands=90, trim_tail_words=2, cold_open={"start_sec": 9.65, "end_sec": 12.05})],
        candidate_map={"c1": _candidate()}, analysis=a,
    )
    assert reel.overall == pytest.approx(base.overall + 4.0)  # (90 - 50) * 0.1
    assert reel.ending_lands == 90 and reel.tail_trim_words == 2
    assert reel.cold_open == (9.65, 12.05)
    (bad,) = _coerce_rankings(
        [_entry(ending_lands=400, trim_tail_words=-1, cold_open={"start_sec": 1, "end_sec": 3})],
        candidate_map={"c1": _candidate()}, analysis=a,
    )
    assert bad.ending_lands is None and bad.tail_trim_words is None and bad.cold_open is None
    assert bad.overall == base.overall


# ---- trailing filler ----------------------------------------------------------------------


def _config(**kw) -> SelectionConfig:
    return SelectionConfig(target_min_sec=kw.get("lo", 5.0), target_max_sec=60.0, event_guard=False)


def test_trailing_filler_is_trimmed():
    a = _talk_analysis()
    words = a.transcript.segments[0].words
    reel = _reel([0], 0.0, words[-1].end + 0.3)
    out = trim_trailing(reel, a, _config())
    last_kept = words[-3]  # "fast."
    assert out.end_sec == pytest.approx(last_kept.end + 0.1, abs=0.16)
    assert out.end_sec < words[-2].start + 1e-6  # "so" is gone
    assert out.candidate_id == reel.candidate_id and out.end_trim_sec > 0
    assert out.pre_refine_end_sec == reel.end_sec


def test_model_tail_hint_only_trims_filler():
    words = _speech("this is the whole point of the video right here")
    a = _analysis([_scene(0, 0.0, 10.0)], words)
    reel = _reel([0], 0.0, words[-1].end + 0.2).model_copy(update={"tail_trim_words": 3})
    assert trim_trailing(reel, a, _config()) == reel  # "right here" is content


def test_tail_trim_respects_the_duration_floor():
    a = _talk_analysis()
    words = a.transcript.segments[0].words
    reel = _reel([0], 0.0, words[-1].end + 0.3)
    assert trim_trailing(reel, a, _config(lo=reel.duration_sec - 0.1)) == reel


# ---- compose: cold open --------------------------------------------------------------------


def _reel_with(cold, payoff=60):
    r = _reel([0], 0.0, 40.0)
    return r.model_copy(update={
        "cold_open": cold,
        "scores": ReelScores(narrative_coherence=60, hook_strength=60,
                             emotional_payoff=payoff, standalone_clarity=60),
    })


def test_cold_open_gating():
    cfg = ComposeConfig()
    cold = (20.0, 22.5)
    assert cold_open_for(cfg, "hype", _reel_with(cold)) == cold
    assert cold_open_for(cfg, "talking_head", _reel_with(cold)) is None
    assert cold_open_for(cfg, "talking_head", _reel_with(cold, COLD_OPEN_PAYOFF_MIN)) == cold
    assert cold_open_for(cfg, "cinematic", _reel_with(cold, 99)) is None
    assert cold_open_for(ComposeConfig(cold_open="off"), "hype", _reel_with(cold)) is None
    assert cold_open_for(ComposeConfig(cold_open="on"), "chill", _reel_with(cold)) == cold
    assert cold_open_for(cfg, "hype", _reel_with(None)) is None


def test_cold_open_leads_the_plan_with_a_locked_hard_cut():
    a = _analysis([_scene(0, 0.0, 20.0), _scene(1, 20.0, 40.0)], None)
    bounds = with_cold_open([(0, 0.0, 20.0), (1, 20.0, 40.0)], (25.0, 27.5), a)
    assert bounds[0] == (1, 25.0, 27.5)
    plan = plan_edit("cinematic", bounds, _reel([0, 1], 0.0, 40.0), a, ComposeConfig(), None)
    locked = lock_cold_open(plan, (25.0, 27.5))
    assert locked.cold_open_shots == 1 and locked.per_cut[0] == ("cut", 0.04)
    # The director can neither nudge the cold open nor soften its exit cut.
    raw = {"shots": [{"index": 0, "nudge_start_sec": 1.0, "reason": "x"}],
           "cuts": [{"index": 0, "kind": "fade"}], "hook_text": None}
    directed, _, applied = apply_director(locked, raw, a)
    assert directed.shots[0].in_ts == 25.0 and directed.per_cut[0] == ("cut", 0.04)
    assert directed.cold_open_shots == 1 and applied == []


def test_lock_needs_a_leading_cold_shot():
    plan = EditPlan(style="classic", shots=[PlannedShot(0, 0.0, 5.0), PlannedShot(0, 5.0, 9.0)],
                    per_cut=[None])
    assert lock_cold_open(plan, (30.0, 32.0)) == plan


def test_cold_open_exit_gets_a_whoosh():
    from pathlib import Path

    from reelforge_core.compose.clips import ClipInfo

    clips = [ClipInfo(path=Path(f"/c{i}"), scene_index=0, in_ts=0, out_ts=d, duration=d,
                      has_audio=True, effects_applied=[]) for i, d in enumerate((2.5, 10.0, 10.0))]
    transitions = [("cut", 0.04), ("cut", 0.04)]
    plan = EditPlan(style="hype", shots=[], per_cut=[], cold_open=(1.0, 3.5), cold_open_shots=1)
    exits = cold_open_exit(clips, transitions, plan)
    assert exits == [pytest.approx(2.48)]
    cues = plan_sfx(total=22.4, pops=[], durations=[2.5, 10.0, 10.0], transitions=transitions,
                    layer_starts=[], whooshes=exits)
    assert cues == [(pytest.approx(2.13), "whoosh")]
    assert cold_open_exit(clips, transitions, None) == []
