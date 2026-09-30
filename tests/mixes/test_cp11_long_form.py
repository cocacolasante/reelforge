"""Pro-editing CP11: long-form retention — chapters, keep priorities and
sentence-level length fitting, the intro montage, the midpoint re-hook,
chapters in compose, and a track change at chapter boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.music_analysis import TrackAnalysis, chapter_bed
from reelforge_core.compose.pipeline import _chapter_times
from reelforge_core.mixes.planner import format_chapters, plan_long_form, youtube_chapters
from reelforge_core.mixes.sequencer import (
    RECORD_MIX_LONG,
    SequencedMix,
    _sentence_text,
    sequence_mix,
    validate_intro,
    validate_sequence,
)
from reelforge_core.models import (
    AnalysisConfig,
    AnalysisReport,
    Chapter,
    ReelTimeline,
    Scene,
    TimelineShot,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from reelforge_core.publish.credits import music_credit_for_reel
from tests.mixes.test_sequencer import _moment

A1, A2 = "a" * 64, "b" * 64


def _talk(aid: str, seconds: float = 600.0) -> AnalysisReport:
    """Continuous speech: a word every 0.4s, a sentence end every 8 words."""
    words, t, n = [], 0.2, 0
    while t + 0.3 < seconds:
        n += 1
        words.append(TranscriptWord(start=round(t, 2), end=round(t + 0.3, 2),
                                    word=f" w{n}{'.' if n % 8 == 0 else ''}", probability=1.0))
        t += 0.4
    return AnalysisReport(
        asset_id=aid, source_path=f"/x/{aid}.mp4", duration=seconds, width=1920, height=1080,
        fps=30.0, has_audio=True, config=AnalysisConfig(),
        scenes=[Scene(index=0, start_sec=0.0, end_sec=seconds, start_frame=0,
                      end_frame=int(seconds * 30), thumbnail_path="t.jpg")],
        transcript=Transcript(language="en", language_probability=1.0, duration=seconds,
                              segments=[TranscriptSegment(start=0.0, end=seconds, text="t", words=words)]),
        loudness=[], semantics=[], created_at="2026-01-01T00:00:00+00:00", elapsed_sec=0.0,
        reelforge_version="0", anthropic_usage={},
    )


def _long_pool():
    pool = [_moment(A1, f"m{i}", i * 60.0, dur=55.0, score=50 - i) for i in range(8)]
    pool += [_moment(A2, f"n{i}", i * 60.0, dur=55.0, score=40 - i) for i in range(4)]
    return pool, {A1: _talk(A1), A2: _talk(A2, 300.0)}


# ---- sequencer --------------------------------------------------------------------------


def test_long_tool_has_the_retention_fields():
    item = RECORD_MIX_LONG["input_schema"]["properties"]["sequence"]["items"]["properties"]
    assert {"keep_priority", "chapter_title"} <= set(item)
    assert {"intro_lines", "rehook_text"} <= set(RECORD_MIX_LONG["input_schema"]["properties"])


def test_sentence_text_cuts_at_a_sentence_end():
    words = [f"w{i}{'.' if i % 8 == 7 else ''}" for i in range(40)]
    out = _sentence_text(words, 20)
    assert out.endswith("w15.") and len(out.split()) == 16
    assert _sentence_text(words[:5], 20) == " ".join(words[:5])


def test_long_form_validation_keeps_structure():
    pool, analyses = _long_pool()
    raw = {
        "sequence": [
            {"moment_id": "m0", "keep_priority": 5, "chapter_title": "Why it matters"},
            {"moment_id": "m1", "keep_priority": 2},
            {"moment_id": "n0", "keep_priority": 4, "chapter_title": "The fix"},
            {"moment_id": "m2", "keep_priority": 9, "chapter_title": "  "},
            {"moment_id": "n1", "keep_priority": 5, "chapter_title": "Results"},
        ],
        "title": "T", "hook": "H", "suggested_mood": "calm", "content_style": "talking_head",
        "intro_lines": [
            {"moment_id": "n1", "start_sec": 70.2, "end_sec": 74.0},
            {"moment_id": "m2", "start_sec": 130.2, "end_sec": 133.8},
        ],
        "rehook_text": "the one tool that changed everything",
    }
    mix = validate_sequence(raw, pool, 280.0, analyses, long_form=True)
    assert mix.priorities == [5, 2, 4, 3, 5]  # out-of-range -> 3
    assert mix.chapter_titles == ["Why it matters", None, "The fix", None, "Results"]
    assert len(mix.intro) == 2 and mix.intro[0][0] == A2
    assert mix.rehook_text.startswith("the one tool")


def test_long_form_trims_sentences_before_dropping_sections():
    pool, analyses = _long_pool()
    seq = [{"moment_id": f"m{i}", "keep_priority": p} for i, p in enumerate([5, 1, 4, 2, 5])]
    raw = {"sequence": seq, "title": "T", "hook": "", "suggested_mood": "calm",
           "content_style": "talking_head"}
    # 5 x 55s = 275s against a 200s target (240s with the band): trimming
    # sentences off the low-priority sections is enough — nothing is dropped.
    mix = validate_sequence(raw, pool, 200.0, analyses, long_form=True)
    assert len(mix.shots) == 5
    lengths = [o - i for _, i, o in mix.shots]
    assert sum(lengths) <= 240.0 + 1e-6
    # Priority 5 untouched (bar the word-safe edge snap).
    assert lengths[0] == pytest.approx(55.0, abs=0.3) and lengths[4] == pytest.approx(55.0, abs=0.3)
    assert lengths[1] < 50.0  # priority 1 trimmed first
    # Every trimmed end sits on a sentence end (+ the unit's own bound).
    words = analyses[A1].transcript.segments[0].words
    ends = {round(w.end, 2) for w in words if w.word.endswith(".")}
    assert all(round(o, 2) in ends for (_, i, o), ln in zip(mix.shots, lengths) if ln < 50.0)


def test_long_form_drops_lowest_priority_but_never_the_ends():
    pool, analyses = _long_pool()
    seq = [{"moment_id": f"m{i}", "keep_priority": p} for i, p in enumerate([1, 5, 1, 5, 1, 5, 1])]
    raw = {"sequence": seq, "title": "T", "hook": "", "suggested_mood": "calm",
           "content_style": "talking_head"}
    mix = validate_sequence(raw, pool, 100.0, analyses, long_form=True)
    starts = [i for _, i, _ in mix.shots]
    # First + last (both priority 1) survive (edges snapped off words).
    assert starts[0] == pytest.approx(0.0, abs=0.3) and starts[-1] == pytest.approx(360.0, abs=0.3)
    assert len(mix.shots) >= 3


def test_intro_lines_are_validated():
    pool, analyses = _long_pool()
    by_id = {m.moment_id: m for m in pool}
    lines = [
        {"moment_id": "m1", "start_sec": 62.2, "end_sec": 66.0},  # fine
        {"moment_id": "m1", "start_sec": 63.0, "end_sec": 65.0},  # overlaps the first
        {"moment_id": "m2", "start_sec": 125.0, "end_sec": 145.0},  # too long
        {"moment_id": "zz", "start_sec": 1.0, "end_sec": 3.0},  # unknown
        {"moment_id": "m3", "start_sec": 182.2, "end_sec": 186.0},  # fine
    ]
    # 66.0 / 186.0 fall inside a word: snapped out to its end.
    assert validate_intro(lines, by_id, analyses) == [(A1, 62.2, 66.1), (A1, 182.2, 186.1)]
    assert validate_intro(lines[:1], by_id, analyses) == []  # one line is no montage


@pytest.mark.asyncio
async def test_long_form_call_uses_the_long_tool_and_section_text():
    pool, analyses = _long_pool()
    seen = {}

    class Client:
        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")

    mix, _ = await sequence_mix(pool, analyses, {A1: "a.mp4", A2: "b.mp4"}, target_sec=600.0,
                                model="claude-opus-5-5", client=Client())
    assert mix.fallback  # the failure falls back, as always
    assert seen["tools"][0] is RECORD_MIX_LONG and "RETENTION STRUCTURE" in seen["system"]
    texts = [json.loads(b["text"]) for b in seen["messages"][0]["content"][1:] if b["type"] == "text"]
    assert all(len(t.get("transcript_text", "").split()) <= 400 for t in texts)
    assert any(len(t.get("transcript_text", "").split()) > 80 for t in texts)
    assert "temperature" not in json.dumps(seen.get("extra_body", {}))  # opus: no sampling params


# ---- chapters -----------------------------------------------------------------------------


def test_youtube_chapter_rules():
    got = youtube_chapters([("Intro", 3.0), ("Setup", 40.0), ("Too soon", 45.0), ("Fix", 100.0),
                            ("Tail", 205.0)], 210.0)
    assert got == [("Intro", 0.0), ("Setup", 40.0), ("Fix", 100.0)]
    assert youtube_chapters([("A", 0.0), ("B", 50.0)], 200.0) == []  # fewer than 3
    assert format_chapters([("Intro", 0.0), ("Fix", 95.0), ("End", 3725.0)]) == "0:00 Intro\n1:35 Fix\n1:02:05 End"


def test_plan_long_form_intro_chapters_and_overlays():
    analyses = {A1: _talk(A1)}
    seq = SequencedMix(
        shots=[(A1, 0.0, 60.0), (A1, 70.0, 130.0), (A1, 140.0, 200.0), (A1, 210.0, 270.0)],
        title="T", hook="", suggested_mood="calm", content_style="classic",
        priorities=[5, 3, 3, 5],
        chapter_titles=["Setup", None, "The fix", "Results"],
        intro=[(A1, 150.2, 154.0), (A1, 250.2, 254.0)],
        rehook_text="what the numbers showed",
    )
    tl = plan_long_form(seq, analyses, "classic", None)
    # The montage cold-opens, then a hard cut into the story.
    assert (tl.shots[0].in_ts, tl.shots[1].in_ts) == (150.2, 250.2)
    assert tl.shots[1].transition_after.kind == "cut"
    titles = [c.title for c in tl.chapters]
    assert titles[0] == "Intro" and "The fix" in titles and tl.chapters[0].shot_index == 0
    cards = [o for o in tl.overlays if o.id.startswith("chapter-")]
    assert len(cards) == len(tl.chapters) - 1 and all(o.position == "top" for o in cards)
    rehook = [o for o in tl.overlays if o.id == "rehook"]
    assert rehook and rehook[0].text == "Coming up: what the numbers showed"
    total = sum(s.duration for s in tl.shots)
    assert 0.3 * total < rehook[0].start_sec < 0.7 * total


def test_timeline_drops_chapters_past_the_shots():
    tl = ReelTimeline(
        shots=[TimelineShot(kind="video", asset_id=A1, in_ts=0, out_ts=5)],
        chapters=[Chapter(title="A", shot_index=0), Chapter(title="B", shot_index=3)],
    )
    assert [c.title for c in tl.chapters] == ["A"]


def test_compose_chapter_times_use_the_render_math():
    clips = [ClipInfo(path=Path(f"/c{i}"), scene_index=0, in_ts=0, out_ts=d, duration=d,
                      has_audio=True, effects_applied=[]) for i, d in enumerate((30.0, 40.0, 50.0, 60.0))]
    tl = SimpleNamespace(chapters=[Chapter(title="Intro", shot_index=0), Chapter(title="Two", shot_index=1),
                                   Chapter(title="Three", shot_index=3)])
    got = _chapter_times(tl, clips, [0.5, 0.5, 0.5])
    # 30 - 0.5, then + 40 - 0.5 + 50 - 0.5.
    assert got == [("Intro", 0.0), ("Two", 29.5), ("Three", pytest.approx(118.5))]


# ---- music bed ------------------------------------------------------------------------------


def _ta(duration: float) -> TrackAnalysis:
    return TrackAnalysis(bpm=120.0, phase_sec=0.0, downbeat_sec=0.0, duration_sec=duration,
                         phrase_sec=16.0, phrase_starts=[16.0 * i for i in range(int(duration // 16))],
                         phrase_energy=[1.0] * int(duration // 16), drop_sec=None)


def test_bed_changes_track_at_a_chapter_boundary():
    tracks = [("/m/one.mp3", _ta(180.0)), ("/m/two.mp3", _ta(200.0))]
    segs = chapter_bed(tracks, [0.0, 90.0, 150.0, 260.0, 400.0], 480.0)
    paths = [p for _, _, p in segs]
    assert paths[0] == "/m/one.mp3" and "/m/two.mp3" in paths
    # The switch happens at a chapter start: the first track plays 0-90s
    # (+ the crossfade) — running on to 150s would pass 70% of its 180s.
    first_group = sum(e - s for s, e, _ in segs[: paths.index("/m/two.mp3")])
    assert first_group == pytest.approx(90.0 + 2.0, abs=0.01)


def test_credits_cover_every_bed_track(tmp_path):
    rd = tmp_path / "working" / A1 / "reels" / "mix-1"
    rd.mkdir(parents=True)
    by = lambda n: {"license": "CC-BY-4.0", "attribution": f"{n} by Scott Buckley"}  # noqa: E731
    (rd / "compose.json").write_text(json.dumps({
        "chosen_music": by("Jul"),
        "music_section": {"tracks": [by("Jul"), by("Aurora"), {"license": "CC0", "attribution": "x"}]},
    }))
    assert music_credit_for_reel(A1, "mix-1", tmp_path) == "Music: Jul by Scott Buckley\nMusic: Aurora by Scott Buckley"


def test_rehook_prefix_is_not_doubled():
    pool, analyses = _long_pool()
    raw = {"sequence": [{"moment_id": f"m{i}"} for i in range(3)], "title": "T", "hook": "",
           "suggested_mood": "calm", "content_style": "classic",
           "rehook_text": "Coming up: the steps, the climb out"}
    assert validate_sequence(raw, pool, 170.0, analyses, long_form=True).rehook_text == "the steps, the climb out"


@pytest.mark.asyncio
async def test_progress_reader_survives_carriage_return_only_output(tmp_path):
    """ffmpeg -stats redraws with \\r only: a 6-min render once overflowed
    asyncio's 64 KB readline limit and crashed the compose."""
    import sys

    from reelforge_core.compose.pipeline import _run_ffmpeg_with_progress

    script = (
        "import sys\n"
        "for i in range(4000):\n"
        "    sys.stderr.write('frame=%d time=00:%02d:%02d.00 ' % (i, i // 60 % 60, i % 60) + 'x' * 80 + '\\r')\n"
    )
    seen = []

    async def progress(ev):
        seen.append(ev.stage_progress)

    await _run_ffmpeg_with_progress([sys.executable, "-c", script], log_file=tmp_path / "log.txt",
                                    total_duration_sec=3000.0, progress=progress)
    # Progress is throttled to 0.5s, but reaching 100% always emits: the
    # reader parsed its way through ~400 KB of carriage-return-only output.
    assert seen and max(seen) == 1.0


def test_long_shots_keep_moving_within_eight_seconds():
    analyses = {A1: _talk(A1)}
    seq = SequencedMix(shots=[(A1, 0.0, 90.0), (A1, 100.0, 190.0), (A1, 200.0, 290.0)],
                       title="T", hook="", suggested_mood="calm", content_style="chill",
                       chapter_titles=["A", "B", "C"])
    tl = plan_long_form(seq, analyses, "chill", None)
    for sh in tl.shots:
        times = [k[0] for k in sh.framing_keys] + [sh.duration]
        assert max(b - a for a, b in zip(times, times[1:])) <= 8.0 + 1e-6
        zooms = [k[1] for k in sh.framing_keys]
        assert all(a != b for a, b in zip(zooms, zooms[1:]))


def test_hype_long_form_keeps_its_own_cutting():
    analyses = {A1: _talk(A1)}
    seq = SequencedMix(shots=[(A1, 0.0, 30.0), (A1, 40.0, 70.0), (A1, 80.0, 110.0)],
                       title="T", hook="", suggested_mood="calm", content_style="hype")
    tl = plan_long_form(seq, analyses, "hype", None)
    assert all(len(sh.framing_keys) <= 1 for sh in tl.shots)
