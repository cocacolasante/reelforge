"""QA metrics: what counts as a visible change, and the speech/ending checks."""

from __future__ import annotations

import pytest

from reelforge_core.compose.safezone import Rect, safe_rect
from reelforge_core.qa import metrics as m
from reelforge_core.qa.captions_geom import CaptionBox
from reelforge_core.qa.thresholds import BY_KIND, Target


def _shot(asset="a", in_ts=0.0, out_ts=3.0, zoom=1.0, x=0.04, photo=False):
    return m.Shot(asset, in_ts, out_ts, out_ts - in_ts if in_ts is not None else 3.0, zoom, photo, x)


# --- visible change, from the plan ----------------------------------------------


def test_contiguous_same_framing_join_is_invisible():
    """The skate-reel case: one continuous shot cut into pieces changes nothing."""
    kind = m.classify_junction(_shot(in_ts=5.0, out_ts=7.4), _shot(in_ts=7.05, out_ts=10.0))
    assert kind == "invisible"


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (_shot("a"), _shot("b"), "cut"),                                   # different clip
        (_shot(in_ts=0, out_ts=3), _shot(in_ts=4, out_ts=6), "jump"),       # 1s skip
        (_shot(in_ts=0, out_ts=3), _shot(in_ts=3, out_ts=6, zoom=1.25), "reframe"),
        (_shot(in_ts=0, out_ts=3), _shot(in_ts=3, out_ts=6, zoom=1.05), "invisible"),
        (_shot(), _shot(photo=True), "cut"),
    ],
)
def test_junction_classification(a, b, expected):
    assert m.classify_junction(a, b) == expected


def test_junction_times_subtract_crossfade_overlap():
    shots = [_shot(in_ts=0, out_ts=4, x=0.4), _shot("b", in_ts=0, out_ts=4, x=0.4), _shot("c")]
    # first junction: shot 2 starts at 4 - 0.4 = 3.6, midpoint 3.8
    assert [round(t, 2) for t in m.junction_times(shots)] == [3.8, 7.4]


def test_changes_per_minute_and_longest_static():
    shots = [_shot("a", 0, 3), _shot("a", 3, 6), _shot("b", 0, 3), _shot("b", 10, 13)]
    changes, invisible = m.visible_changes(shots, duration=12.0)
    assert invisible == 1
    assert [c.why for c in changes] == ["cut", "jump"]
    assert m.changes_per_minute(changes, 12.0) == 10.0
    # changes at ~6s and ~9s: the longest unbroken stretch is the first 6s
    assert m.longest_static(changes, 12.0) == pytest.approx(5.94, abs=0.05)


def test_broll_windows_count_as_changes_in_and_out():
    changes, _ = m.visible_changes([_shot(in_ts=0, out_ts=20)], layers=[(4.0, 7.0)], duration=20.0)
    assert [(c.t, c.why) for c in changes] == [(4.0, "broll"), (7.0, "broll")]


# --- speech ------------------------------------------------------------------------


def _words(*items):
    return [m.Word(s, e, w) for s, e, w in items]


def test_hook_latency_and_greeting():
    words = _words((0.9, 1.2, "Hey"), (1.2, 1.5, "guys,"), (1.5, 1.9, "welcome"), (1.9, 2.2, "back."))
    assert m.hook_latency(words) == 0.9
    assert m.greeting_in_opening(words) is not None
    clean = _words((0.1, 0.4, "Stop"), (0.4, 0.8, "overpaying."))
    assert m.greeting_in_opening(clean) is None


def test_dead_air_counts_stalls_between_words():
    words = _words((0.0, 0.5, "one"), (0.6, 1.0, "two"), (2.0, 2.5, "three"), (2.6, 3.0, "four"))
    air = m.dead_air(words)
    assert air["count"] == 1 and air["longest"] == 1.0
    assert air["percent"] == pytest.approx(33.3, abs=0.1)


def test_filler_rate_counts_words_and_phrases():
    words = _words(*[(i, i + 0.3, w) for i, w in enumerate(["um", "so", "you", "know", "uh", "right"])])
    assert m.filler_rate(words, 60.0) == 3.0  # um, you know, uh


@pytest.mark.parametrize(
    "closing,trailing",
    [("and that is how you do it.", False), ("so yeah", True), ("anyway,", True), ("that's it", True)],
)
def test_ending_flags_trailing_filler(closing, trailing):
    words = [m.Word(i, i + 0.4, w) for i, w in enumerate(closing.split())]
    assert m.ending(words, 30.0)["trailing_filler"] is trailing


def test_ending_reports_whether_it_lands_on_a_sentence():
    assert m.ending(_words((0, 1, "Done.")), 5.0)["ends_on_sentence"] is True
    assert m.ending(_words((0, 1, "and")), 5.0)["ends_on_sentence"] is False
    assert m.ending([], 5.0) == {"applies": False}


@pytest.mark.parametrize(
    "duration,ratio,style,kind",
    [(40, 0.6, None, "talking"), (40, 0.05, "hype", "action"), (40, 0.1, "talking_head", "talking"),
     (400, 0.7, None, "long_form")],
)
def test_content_kind(duration, ratio, style, kind):
    assert m.content_kind(duration, ratio, style) == kind


# --- captions ------------------------------------------------------------------------


def _box(text, rect, style="Default", start=0.0, end=1.0, highlighted=()):
    return CaptionBox(start, end, style, text, 1, len(text.split()), list(highlighted), rect)


def test_caption_stats_group_karaoke_repeats_and_flag_the_ui_band():
    safe = safe_rect(1080, 1920)
    inside = Rect(200, 1100, 880, 1240)
    in_ui = Rect(200, 1500, 880, 1640)  # where lower_third sits today
    boxes = [
        # karaoke: one line, three events, a different word lit in each
        _box("one two three", inside, start=0.0, end=0.4, highlighted=["one"]),
        _box("one two three", inside, start=0.4, end=0.8, highlighted=["two"]),
        _box("one two three", inside, start=0.8, end=1.2, highlighted=["three"]),
        _box("four five", in_ui, start=1.2, end=2.0),
    ]
    stats = m.caption_stats(boxes, safe)
    assert stats["captions"] == 2
    assert stats["words_per_caption_max"] == 3
    assert stats["highlighted_share"] == 0.6  # 3 of 5 words ever lit
    assert stats["safe_zone_violations"] == 1
    assert stats["violations"][0]["text"] == "four five"


def test_safe_zone_is_the_union_on_vertical_and_title_safe_otherwise():
    assert safe_rect(1080, 1920) == Rect(65, 290, 880, 1250)
    assert safe_rect(720, 1280).y1 == pytest.approx(1250 * 1280 / 1920)
    landscape = safe_rect(1920, 1080)
    assert (landscape.x0, landscape.y1) == (96.0, 1026.0)


# --- targets --------------------------------------------------------------------------


def test_target_checks():
    assert Target("x", ">=", 15).check(20) == "pass"
    assert Target("x", "<=", 4.0).check(5.1) == "fail"
    assert Target("x", "between", (0.1, 0.25)).check(0.3) == "fail"
    assert Target("x", "==", 0).check(None) == "n/a"


def test_every_kind_holds_the_common_caption_and_loudness_targets():
    for kind, targets in BY_KIND.items():
        names = {t.metric for t in targets}
        assert {"safe_zone_violations", "integrated_lufs", "true_peak_dbtp"} <= names, kind


# --- review fixes (2026-09-30 baseline) ----------------------------------------


def test_a_slide_between_continuous_pieces_is_a_visible_but_flashy_change():
    a = m.Shot("a", 0.0, 3.0, 3.0, transition_sec=0.4, transition_kind="slideleft")
    b = m.Shot("a", 3.0, 6.0, 3.0)
    assert m.classify_junction(a, b) == "transition"
    plain = m.Shot("a", 0.0, 3.0, 3.0, transition_sec=0.04, transition_kind="fade")
    assert m.classify_junction(plain, b) == "invisible"
    assert m.flashy_share([a, b]) == 1.0
    assert m.flashy_share([b]) is None


def test_one_flashy_transition_per_reel_is_within_budget():
    """The director's single allowed flourish must not fail QA on a short
    reel (1 of 6 joins is 17%, over a flat 10%)."""
    slide = lambda: m.Shot("a", 0.0, 3.0, 3.0, transition_sec=0.25, transition_kind="slideleft")  # noqa: E731
    cut = lambda: m.Shot("a", 0.0, 3.0, 3.0, transition_sec=0.04, transition_kind="cut")  # noqa: E731
    assert m.extra_flashy([slide(), cut(), cut(), cut(), cut(), cut(), cut()]) == 0
    assert m.extra_flashy([slide(), slide(), cut(), cut()]) == 1
    long_reel = [slide() for _ in range(3)] + [cut() for _ in range(28)]  # 30 joins -> 3 allowed
    assert m.extra_flashy(long_reel) == 0


def test_dead_air_ignores_gaps_across_cuts():
    """A demo shot with no talking between two talking shots is an edit, not
    a stall (the tutorial mix scored 46% dead air before this)."""
    words = [
        m.Word(0.0, 0.5, "one", shot=0), m.Word(0.6, 1.0, "two", shot=0),
        m.Word(13.0, 13.5, "three", shot=2), m.Word(14.5, 15.0, "four", shot=2),
    ]
    air = m.dead_air(words)
    assert air["count"] == 1 and air["longest"] == 1.0  # only the in-shot stall
    assert air["percent"] == pytest.approx(1.0 / (1.0 + 2.0) * 100, abs=0.1)
