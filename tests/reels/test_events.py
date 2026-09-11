"""Action events + the cut guard (reels/events.py) and its consumers:
candidate generation, boundary refinement, AI-mix trims, and the final gate."""

from __future__ import annotations

from reelforge_core.mixes.mining import MinedMoment
from reelforge_core.mixes.sequencer import validate_sequence
from reelforge_core.models import (
    EnergyPoint,
    LoudnessPoint,
    RankedReel,
    ReelCandidate,
    ReelScores,
    SelectionConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from reelforge_core.reels import generate_candidates
from reelforge_core.reels.candidates import _candidate_id
from reelforge_core.reels.dedup import enforce_clean_edges
from reelforge_core.reels.events import (
    AFTERMATH_SEC,
    ANTICIPATION_SEC,
    FOLLOW_SEC,
    LEAD_SEC,
    ActionEvent,
    detect_events,
    edge_ok,
    event_position,
    guard_candidates,
    guard_span,
)
from reelforge_core.reels.prescore import PrescoreFeatures
from reelforge_core.reels.refine import apply_refinement

from tests.reels._fixtures import make_analysis

EV = ActionEvent(start_sec=40.0, end_sec=43.0, peak_sec=41.5, strength=5.0)


def _analysis(duration=100.0, motion=None, lufs=None, words=None, asset_id="ev"):
    """Flat motion 10 / loudness -35 LUFS, overridden per bin."""
    n = int(duration)
    motion = motion or {}
    lufs = lufs or {}
    update = {
        "energy": [
            EnergyPoint(time_sec=i + 0.5, motion=motion.get(i, 10.0), loudness_delta=0.0)
            for i in range(n)
        ],
        "loudness": [LoudnessPoint(time_sec=i + 0.5, lufs=lufs.get(i, -35.0)) for i in range(n)],
    }
    if words is not None:
        update["transcript"] = Transcript(
            language="en",
            language_probability=1.0,
            duration=duration,
            segments=[
                TranscriptSegment(
                    start=words[0][0],
                    end=words[-1][1],
                    text=" x",
                    words=[
                        TranscriptWord(start=s, end=e, word=" x", probability=1.0)
                        for s, e in words
                    ],
                )
            ],
        )
    return make_analysis(asset_id, [duration]).model_copy(update=update)


def _event_analysis(asset_id="ev"):
    """A motion burst in bins 40-42 -> one event spanning [40, 43)."""
    return _analysis(motion={40: 60.0, 41: 60.0, 42: 60.0}, asset_id=asset_id)


# ---- detection -------------------------------------------------------------


def test_motion_spike_becomes_an_event():
    events = detect_events(_analysis(motion={20: 60.0, 21: 40.0}))
    assert events == [ActionEvent(start_sec=20.0, end_sec=22.0, peak_sec=20.5, strength=50.0)]


def test_loud_bin_without_motion_is_an_event():
    """A wave hitting the camera can be loud with barely any motion."""
    events = detect_events(_analysis(lufs={30: -20.0}))
    assert [(e.start_sec, e.end_sec) for e in events] == [(30.0, 31.0)]


def test_loud_speech_is_discounted():
    """Someone talking right next to the camera is loud but not action."""
    assert detect_events(_analysis(lufs={30: -20.0}, words=[(30.0, 30.9)])) == []


def test_hysteresis_extends_and_merges_close_events():
    # seed (z=4), extend (z=2.5), gap (z=0), seed (z=6)
    events = detect_events(_analysis(motion={10: 14.0, 11: 12.5, 13: 16.0}))
    assert [(e.start_sec, e.end_sec, e.peak_sec) for e in events] == [(10.0, 14.0, 13.5)]


def test_no_signal_means_no_events():
    """Pre-fix analyses: no energy track and a flat -80 loudness track."""
    a = make_analysis("old", [60.0]).model_copy(
        update={"loudness": [LoudnessPoint(time_sec=i + 0.5, lufs=-80.0) for i in range(60)]}
    )
    assert detect_events(a) == []


def test_event_start_extends_back_through_the_visible_onset():
    """Live case: a wave rising from 189s was only detected at 192s."""
    motion = {188: 11.1, 189: 12.7, 190: 12.0, 191: 11.2, 192: 15.0}  # z = m - 10
    (ev,) = detect_events(_analysis(duration=200.0, motion=motion))
    assert (ev.start_sec, ev.end_sec) == (189.0, 193.0)  # 3s back at most


def test_onset_never_reaches_back_into_the_previous_event():
    motion = {10: 20.0, 11: 11.5, 12: 11.5, 13: 20.0}
    first, second = detect_events(_analysis(motion=motion))
    assert second.start_sec >= first.end_sec


def test_detected_event_bounds_are_ms_precise():
    a = _analysis(motion={98: 60.0, 99: 60.0}).model_copy(update={"duration": 99.6666})
    (ev,) = detect_events(a)
    assert ev.end_sec == 99.667


# ---- the guard -------------------------------------------------------------


def _g(start, end, events=(EV,), min_sec=15.0, max_sec=60.0, duration=100.0, words=()):
    return guard_span(
        start, end, list(events), min_sec=min_sec, max_sec=max_sec, duration=duration, words=words
    )


def test_edge_windows():
    assert not edge_ok(EV.start_sec - ANTICIPATION_SEC + 0.1, "end", [EV], 100.0)
    assert edge_ok(EV.start_sec - ANTICIPATION_SEC, "end", [EV], 100.0)
    assert not edge_ok(EV.end_sec + AFTERMATH_SEC - 0.1, "start", [EV], 100.0)
    assert edge_ok(EV.end_sec + AFTERMATH_SEC, "start", [EV], 100.0)
    # The file's first frame can't cut off a lead-in that doesn't exist.
    assert edge_ok(0.0, "start", [ActionEvent(0.5, 3.0, 1.0, 4.0)], 100.0)


def test_file_end_rounded_to_ms_is_still_the_file_end():
    """Live case: a reel ending at round(duration, 3) was flagged as cutting a
    wipeout that runs to the end of the footage, and the ranker was told the
    event crossed its end."""
    ev = ActionEvent(start_sec=217.0, end_sec=226.326, peak_sec=218.5, strength=22.0)
    assert edge_ok(226.326, "end", [ev], 226.3261)
    assert event_position(ev, 210.5, 226.326) == "inside"


def test_span_clear_of_events_is_untouched():
    g = _g(50.0, 80.0)
    assert (g.status, g.start, g.end) == ("ok", 50.0, 80.0)


def test_end_just_before_an_event_extends_past_it():
    """The "cuts right before the wave crashes" case: include the event."""
    g = _g(10.0, 38.0)
    assert (g.status, g.start, g.end) == ("moved", 10.0, EV.end_sec + FOLLOW_SEC)


def test_end_extension_past_max_duration_pulls_back_instead():
    g = _g(0.0, 38.0, max_sec=40.0)
    assert (g.status, g.start, g.end) == ("moved", 0.0, EV.start_sec - ANTICIPATION_SEC)


def test_start_inside_an_event_moves_back_for_lead_in():
    g = _g(41.0, 70.0)
    assert (g.status, g.start, g.end) == ("moved", EV.start_sec - LEAD_SEC, 70.0)


def test_start_on_the_aftermath_moves_back_to_include_the_event():
    """The "starts right after I've already fallen" case."""
    g = _g(44.0, 70.0)
    assert (g.status, g.start) == ("moved", EV.start_sec - LEAD_SEC)


def test_file_end_is_always_a_legal_end():
    ev = ActionEvent(start_sec=95.0, end_sec=100.0, peak_sec=97.5, strength=4.0)
    g = _g(60.0, 99.0, events=[ev])
    assert (g.status, g.end) == ("moved", 100.0)


def test_unfixable_span_comes_back_unchanged():
    g = _g(38.0, 58.0, min_sec=20.0, max_sec=20.0)
    assert (g.status, g.start, g.end) == ("violation", 38.0, 58.0)


def test_moved_edge_never_lands_mid_word():
    g = _g(41.0, 70.0, words=[(36.8, 37.3)])
    assert (g.status, g.start) == ("moved", 36.8)


def test_mid_word_pull_back_falls_back_to_the_other_word_edge():
    """Snapping the pull-back point to the END of a word lands it back inside
    the no-cut window; the START of that word is legal."""
    g = _g(0.0, 37.5, max_sec=40.0, words=[(35.8, 36.3)])
    assert (g.status, g.end) == ("moved", 35.8)


# ---- consumers ---------------------------------------------------------------


def _cand(asset_id, start, end, source="scene"):
    return ReelCandidate(
        candidate_id=_candidate_id(asset_id, start, end),
        scene_indices=[0],
        start_sec=start,
        end_sec=end,
        duration_sec=end - start,
        scene_count=1,
        source=source,
    )


def test_guard_candidates_reidentifies_and_dedupes():
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=60.0)
    cands = [
        _cand("ev", 41.0, 70.0, "sentence"),
        _cand("ev", 42.0, 70.0, "moment"),  # moves onto the same span -> merged
        _cand("ev", 50.0, 80.0),
    ]
    out = guard_candidates(cands, _analysis(), cfg, events=[EV])
    assert [(c.start_sec, c.end_sec, c.source) for c in out] == [
        (37.0, 70.0, "sentence"),
        (50.0, 80.0, "scene"),
    ]
    assert out[0].candidate_id == _candidate_id("ev", 37.0, 70.0)
    assert out[0].scene_indices == [0]


def test_generate_candidates_guards_edges_unless_disabled():
    a = _event_analysis()
    events = detect_events(a)
    on = generate_candidates(a, SelectionConfig(target_min_sec=15.0, target_max_sec=45.0))
    off = generate_candidates(
        a, SelectionConfig(target_min_sec=15.0, target_max_sec=45.0, event_guard=False)
    )
    assert events and off
    off_spans = {(c.start_sec, c.end_sec) for c in off}
    for c in on:
        legal = edge_ok(c.start_sec, "start", events, a.duration) and edge_ok(
            c.end_sec, "end", events, a.duration
        )
        assert legal or (c.start_sec, c.end_sec) in off_spans  # unfixable: passed through
    assert any((c.start_sec, c.end_sec) not in off_spans for c in on)


def _reel(start, end, cid="r1"):
    return RankedReel(
        candidate_id=cid,
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


def test_refinement_is_moved_off_an_event_within_the_window():
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=60.0)
    out = apply_refinement(_reel(10.0, 40.0), 10.0, 42.0, _event_analysis(), cfg)
    assert (out.start_sec, out.end_sec) == (10.0, 43.0 + FOLLOW_SEC)


def test_refinement_into_an_event_window_is_carried_past_the_event():
    """A proposal ending 3s before the event is moved past it, even though the
    fix lands beyond the model's own ±6s window."""
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=60.0)
    out = apply_refinement(_reel(10.0, 32.0), 10.0, 37.0, _event_analysis(), cfg)
    assert (out.start_sec, out.end_sec) == (10.0, 43.0 + FOLLOW_SEC)


def test_guard_fix_reaches_past_the_refine_window_for_a_long_event():
    """Live case: the model proposed ending past a handstand event, the ±6s
    clamp left the edge mid-event, and the fix (event end + 1.5s) is 13s out."""
    a = _analysis(duration=150.0, motion={b: 60.0 for b in range(111, 126)})  # event [111, 126)
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=50.0)
    out = apply_refinement(_reel(99.25, 114.25), 99.1, 126.0, a, cfg)
    assert (out.start_sec, out.end_sec) == (99.1, 126.0 + FOLLOW_SEC)


def test_refinement_the_guard_cannot_fix_is_rejected():
    cfg = SelectionConfig(target_min_sec=20.0, target_max_sec=20.0)
    reel = _reel(38.0, 58.0)
    out = apply_refinement(reel, 38.5, 58.5, _event_analysis(), cfg)
    assert (out.start_sec, out.end_sec) == (38.0, 58.0)


def test_refinement_guard_can_be_disabled():
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=60.0, event_guard=False)
    out = apply_refinement(_reel(10.0, 40.0), 10.0, 42.0, _event_analysis(), cfg)
    assert out.end_sec == 42.0


def test_final_gate_drops_event_cutting_reels_and_backfills():
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=60.0)
    top = [_reel(10.0, 30.0, "a"), _reel(41.0, 70.0, "b")]  # b opens mid-event
    reserve = [_reel(44.0, 74.0, "c"), _reel(50.0, 80.0, "d")]  # c opens on the aftermath
    out = enforce_clean_edges(top, reserve, cfg, [EV], 100.0)
    assert [r.candidate_id for r in out] == ["a", "d"]


def test_final_gate_keeps_the_list_when_nothing_is_clean():
    cfg = SelectionConfig(target_min_sec=15.0, target_max_sec=60.0)
    top = [_reel(41.0, 70.0)]
    assert enforce_clean_edges(top, [], cfg, [EV], 100.0) == top


def _mm(aid, start, end):
    features = PrescoreFeatures(
        starts_on_unit_boundary=False,
        ends_on_unit_boundary=False,
        starts_mid_word=False,
        ends_mid_word=False,
        speech_ratio=0.0,
        energy_peak_pos=None,
        energy_peak_z=None,
        lufs_range=0.0,
        n_scene_cuts=0,
        source="moment",
    )
    return MinedMoment(asset_id=aid, candidate=_cand(aid, start, end, "moment"), features=features, score=10.0)


def test_mix_trim_that_would_cut_an_event_is_reverted():
    a_id, b_id = "a" * 64, "b" * 64
    pool = [_mm(a_id, 30.0, 36.0), _mm(b_id, 10.0, 16.0), _mm(b_id, 50.0, 56.0)]
    raw = {
        "sequence": [
            # start trim is legal; extending the end by 1s lands 3s before the event
            {"moment_id": pool[0].moment_id, "trim_start_sec": 0.5, "trim_end_sec": -1.0},
            {"moment_id": pool[1].moment_id},
            {"moment_id": pool[2].moment_id},
        ],
        "title": "t",
        "hook": "h",
        "suggested_mood": "energetic",
        "content_style": "hype",
    }
    analyses = {a_id: _event_analysis(a_id), b_id: _analysis(asset_id=b_id)}
    mix = validate_sequence(raw, pool, target_sec=18.0, analyses=analyses)
    assert mix.shots[0] == (a_id, 30.5, 36.0)
