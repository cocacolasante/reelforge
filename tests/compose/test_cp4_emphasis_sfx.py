"""Pro-editing CP4: AI emphasis (key words, key moments, transcript fixes)
and sound effects. The model is always faked; everything it returns goes
through local validation, and any failure falls back to CP2's heuristic."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelforge_core.compose import emphasis as emph
from reelforge_core.compose.captions import build_captions
from reelforge_core.compose.clips import ClipInfo
from reelforge_core.compose.emphasis import (
    Correction,
    Emphasis,
    SpokenWord,
    apply_emphasis,
    build_lines,
    reel_words,
    run_emphasis,
    save_corrections,
    tighten_framing,
    validate_correction,
)
from reelforge_core.compose.graph_builder import build_final_command
from reelforge_core.compose.keywords import pick_keywords
from reelforge_core.compose.sfx import cut_times, plan_sfx, sfx_enabled, sfx_path
from reelforge_core.models import (
    CaptionStyle,
    ComposeConfig,
    EffectsConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from reelforge_core.qa.captions_geom import parse_ass
from tests.compose.test_captions import _analysis_with_transcript, _reel, _scene
from tests.compose.test_graph_builder import _analysis as _graph_analysis

A = "a" * 64


def _sw(i: int, text: str, t: float, shot: int = 0) -> SpokenWord:
    return SpokenWord(A, t, t + 0.3, text, t, shot, t)


def _line(texts: str, t0: float = 0.0, shot: int = 0) -> list[SpokenWord]:
    return [_sw(i, w, t0 + i * 0.4, shot) for i, w in enumerate(texts.split())]


# ---- corrections -------------------------------------------------------------


@pytest.mark.parametrize(
    "old,new,expected",
    [
        ("skinboard", "skimboard", "skimboard"),
        ("skinboard,", "skimboard", "skimboard,"),  # punctuation survives
        ("reelforge", "ReelForge", "ReelForge"),  # casing fix
        ("skinboard", "skim board", None),  # one word for one word
        ("board", "board", None),  # not a change
        ("the", "surfboard", None),  # a rewrite, not a mishearing
        ("", "x", None),
    ],
)
def test_validate_correction(old, new, expected):
    assert validate_correction(old, new, set()) == expected


def test_known_terms_allow_a_bigger_fix():
    assert validate_correction("graph", "GrantMind", set()) is None
    assert validate_correction("graph", "GrantMind", {"grantmind"}) == "GrantMind"


# ---- apply_emphasis -------------------------------------------------------------


def test_apply_emphasis_validates_every_entry():
    lines = [_line("so the first thing is the wax"), _line("use a plastic scraper", 20.0)]
    raw = {
        "lines": [
            {"line": 0, "emphasis": [3, 99, -1, 3, 6], "key_moment": 6, "pop": True},
            {"line": 0, "emphasis": [0]},  # duplicate line: ignored
            {"line": 1, "emphasis": "nope"},  # malformed: skipped
            {"line": 7, "emphasis": [0]},  # no such line
            {"emphasis": [0]},  # no line number
        ],
        "glossary": ["skimboard", "", "x" * 60],
    }
    e = apply_emphasis(raw, lines, [], 30.0)
    words = lines[0]
    assert e.emphasised == {words[6].key, words[3].key}  # km first, then in range
    assert [w.text for w in e.key_moments] == ["wax"]
    assert e.pops == [words[6].mezz]
    assert e.glossary == ["skimboard"]


def test_emphasis_is_capped_per_line():
    line = _line("one two three four five six")
    e = apply_emphasis({"lines": [{"line": 0, "emphasis": [0, 1, 2, 3, 4, 5]}]}, [line], [], 10.0)
    assert len(e.emphasised) == 3  # ceil(6 * 0.34)


def test_key_moments_are_spaced_and_pops_follow_them():
    lines = [_line("a big win", t) for t in (0.0, 2.0, 7.0, 9.0)]
    raw = {"lines": [{"line": i, "emphasis": [], "key_moment": 1, "pop": True} for i in range(4)]}
    e = apply_emphasis(raw, lines, [], 30.0)
    assert [round(w.mezz, 1) for w in e.key_moments] == [0.4, 7.4]
    assert e.pops == [w.mezz for w in e.key_moments]


def test_corrections_are_capped():
    line = _line(" ".join(f"skinboard{i}" for i in range(40)))
    raw = {"lines": [{"line": 0, "emphasis": [], "corrections": [
        {"index": i, "text": f"skimboard{i}"} for i in range(40)]}]}
    e = apply_emphasis(raw, [line], [], 20.0)
    assert len(e.corrections) == 4  # 10% of 40 words


def test_build_lines_breaks_at_sentences_pauses_and_shots():
    words = _line("hi there. so this") + [_sw(9, "next", 5.0), _sw(10, "shot", 5.3, shot=1)]
    assert [[w.text for w in ln] for ln in build_lines(words)] == [
        ["hi", "there."], ["so", "this"], ["next"], ["shot"]]


# ---- captions --------------------------------------------------------------------


def test_preferred_keywords_replace_the_heuristic():
    chunks = [["never", "use"], ["a", "metal"], ["scraper", "on"], ["the", "board"],
              ["it", "gouges"], ["$400", "later"]]
    ai = [[False, False], [False, True], [False, False], [False, False],
          [False, False], [False, False]]
    got = pick_keywords(chunks, preferred=ai)
    # The AI's pick, topped up to the 10% floor (2 of 12) with the best
    # heuristic word — not the heuristic's own full list ("never", "$400").
    assert (1, 1) in got and len(got) == 2
    assert got - {(1, 1)} <= {(0, 0), (5, 0)}


def _speech(texts: list[str]) -> list[TranscriptWord]:
    out, t = [], 0.2
    for w in texts:
        out.append(TranscriptWord(start=round(t, 3), end=round(t + 0.28, 3), word=" " + w, probability=0.9))
        t += 0.32
    return out


def test_captions_highlight_the_ai_key_words(tmp_path):
    words = _speech("grab a plastic scraper and work from the nose toward the tail".split())
    analysis = _analysis_with_transcript(words, [_scene(0, 0.0, 30.0)])
    target = words[2]  # "plastic" — not something the heuristic would pick
    e = Emphasis(emphasised=frozenset({(analysis.asset_id, round(target.start * 1000))}), source="ai")
    cfg = ComposeConfig(captions=CaptionStyle(mode="punch"))
    path = build_captions(_reel([0], 0.0, 30.0), analysis, cfg, tmp_path, emphasis=e)
    lit = [w for b in parse_ass(path).boxes for w in b.highlighted]
    assert "plastic" in lit


# ---- framing --------------------------------------------------------------------


def _keyed_clip(keys) -> ClipInfo:
    return ClipInfo(path=Path("/tmp/c.mp4"), scene_index=0, in_ts=0.0, out_ts=12.0,
                    duration=12.0, has_audio=True, effects_applied=[], framing_keys=keys)


def test_key_moment_tightens_its_phrase_without_moving_keys():
    keys = ((0.0, 1.0, 0.5, 0.42), (3.0, 1.15, 0.5, 0.42), (6.0, 1.3, 0.5, 0.42), (9.0, 1.0, 0.5, 0.42))
    plain = replace(_keyed_clip(()), framing_keys=())
    moment = SpokenWord(A, 1.5, 1.8, "win", 1.5, 0, 1.5)
    out = tighten_framing([_keyed_clip(keys), plain], Emphasis(key_moments=[moment]))
    assert [k[0] for k in out[0].framing_keys] == [0.0, 3.0, 6.0, 9.0]
    assert [k[1] for k in out[0].framing_keys] == [1.3, 1.15, 1.3, 1.0]
    assert out[1].framing_keys == ()
    # A moment right after a tight stretch is left alone (no invisible change).
    late = SpokenWord(A, 9.5, 9.8, "win", 9.5, 0, 9.5)
    same = tighten_framing([_keyed_clip(keys)], Emphasis(key_moments=[late]))
    assert same[0].framing_keys == keys


def test_tight_key_followed_by_tight_key_is_relaxed():
    keys = ((0.0, 1.0, 0.5, 0.42), (3.0, 1.3, 0.5, 0.42))
    moment = SpokenWord(A, 1.0, 1.3, "win", 1.0, 0, 1.0)
    out = tighten_framing([_keyed_clip(keys)], Emphasis(key_moments=[moment]))
    assert [k[1] for k in out[0].framing_keys] == [1.3, 1.0]


# ---- word gathering ---------------------------------------------------------------


def test_reel_words_map_to_mezzanine_time_across_a_crossfade(tmp_path):
    words = _speech(["hello", "there", "friend"])
    analysis = _analysis_with_transcript(words, [_scene(0, 0.0, 30.0)])
    clips = [
        ClipInfo(path=Path("/a"), scene_index=0, in_ts=0.0, out_ts=0.5, duration=0.5,
                 has_audio=True, effects_applied=[], asset_id=analysis.asset_id),
        ClipInfo(path=Path("/b"), scene_index=0, in_ts=0.5, out_ts=2.0, duration=1.5,
                 has_audio=True, effects_applied=[], asset_id=analysis.asset_id),
    ]
    got = reel_words(clips, analysis, None, [0.2])
    assert [(w.text, w.shot) for w in got] == [("hello", 0), ("there", 1), ("friend", 1)]
    # "there" starts 0.52s in the source = 0.02s into shot 1, which starts at
    # 0.5 - 0.2 (crossfade overlap) on the mezzanine.
    assert got[1].mezz == pytest.approx(0.32) and got[1].offset == pytest.approx(0.02)


# ---- transcript fixes ------------------------------------------------------------------


def test_save_corrections_patches_the_override(monkeypatch):
    from reelforge_core import transcript_store

    saved = {}
    monkeypatch.setattr(transcript_store, "_save_sync", lambda aid, t: saved.__setitem__(aid, t))
    words = _speech(["clean", "your", "skinboard,", "then"])
    analysis = _analysis_with_transcript(words, [_scene(0, 0.0, 30.0)])
    fix = Correction(analysis.asset_id, round(words[2].start * 1000), "skinboard,", "skimboard,")
    assert save_corrections([fix], {analysis.asset_id: analysis}) == 1
    t = saved[analysis.asset_id]
    assert t.segments[0].words[2].word == " skimboard,"
    assert "skimboard," in t.segments[0].text and "skinboard" not in t.segments[0].text
    # Replaying against the already-fixed transcript changes nothing.
    fixed = analysis.model_copy(update={"transcript": t})
    saved.clear()
    assert save_corrections([fix], {analysis.asset_id: fixed}) == 0 and not saved


# ---- the stamped call ---------------------------------------------------------------------


class _FakeClient:
    def __init__(self, payload=None, fail=False):
        self.calls = 0
        self.messages = self
        self.payload = payload
        self.fail = fail

    async def create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=self.payload)],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )


def _reel_obj():
    return _reel([0], 0.0, 30.0)


@pytest.mark.asyncio
async def test_run_emphasis_is_stamped_including_after_its_own_fixes(tmp_path):
    words = _line("wax your skinboard in the sun")
    payload = {"lines": [{"line": 0, "emphasis": [5], "key_moment": 2,
                          "corrections": [{"index": 2, "text": "skimboard"}]}],
               "glossary": ["skimboard"]}
    working = tmp_path / "working"
    (working / A).mkdir(parents=True)
    client = _FakeClient(payload)
    e, usage = await run_emphasis(words, reel=_reel_obj(), model="m", reel_dir=tmp_path,
                                  working_root=working, duration=10.0, client=client)
    assert e.source == "ai" and usage.input_tokens == 100
    assert [(c.old, c.new) for c in e.corrections] == [("skinboard", "skimboard")]
    assert json.loads((working / A / "glossary.json").read_text())["terms"] == ["skimboard"]
    # Same words: no second call.
    again, _ = await run_emphasis(words, reel=_reel_obj(), model="m", reel_dir=tmp_path,
                                  working_root=working, duration=10.0, client=client)
    assert client.calls == 1 and again.source == "cache" and again.emphasised == e.emphasised
    # After the fix is saved the transcript reads "skimboard" — still a hit.
    fixed = [replace(w, text="skimboard") if w.text == "skinboard" else w for w in words]
    after, _ = await run_emphasis(fixed, reel=_reel_obj(), model="m", reel_dir=tmp_path,
                                  working_root=working, duration=10.0, client=client)
    assert client.calls == 1 and after.source == "cache" and after.corrections == []
    # A different model is a different stamp.
    await run_emphasis(fixed, reel=_reel_obj(), model="other", reel_dir=tmp_path,
                       working_root=working, duration=10.0, client=client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_run_emphasis_failure_means_heuristic(tmp_path):
    e, _ = await run_emphasis(_line("hello there"), reel=_reel_obj(), model="m", reel_dir=tmp_path,
                              working_root=tmp_path, duration=5.0, client=_FakeClient(fail=True))
    assert e.source == "none" and not (tmp_path / "emphasis_raw.json").exists()
    # And the default client is blocked in tests (tests/conftest.py).
    e2, _ = await run_emphasis(_line("hello there"), reel=_reel_obj(), model="m", reel_dir=tmp_path,
                               working_root=tmp_path, duration=5.0)
    assert e2.source == "none"


# ---- sound effects ----------------------------------------------------------------------------


def test_cut_times_are_crossfade_midpoints():
    assert cut_times([5.0, 5.0, 5.0], [("cut", 0.04), ("slideleft", 0.4)]) == [(4.98, "cut"), (9.76, "slideleft")]


def test_plan_sfx_is_sparse_and_prefers_whooshes():
    cues = plan_sfx(
        total=30.0,
        pops=[2.0, 5.0, 12.0, 25.0, 29.8],
        durations=[10.0, 10.0, 10.4],
        transitions=[("cut", 0.04), ("slideleft", 0.4)],
        layer_starts=[13.0],
    )
    kinds = [k for _, k in cues]
    starts = [t for t, _ in cues]
    # The whoosh into the B-roll at 13s wins over the pop at 12s; the hard
    # cut at ~10s stays dry; the slide at ~19.8s gets a whoosh, which then
    # crowds out the pop at 25s; the pop at 29.8s is too close to the end.
    assert kinds == ["pop", "whoosh", "whoosh"]
    assert starts[0] == 2.0 and starts[1] == pytest.approx(13.0 - 0.35)
    assert all(b - a >= 6.0 - 0.35 for a, b in zip(starts, starts[1:]))


def test_plan_sfx_long_form_gap_and_cap():
    cues = plan_sfx(total=600.0, pops=[float(t) for t in range(5, 595, 3)],
                    durations=[600.0], transitions=[], layer_starts=[])
    assert len(cues) <= 30 and all(b[0] - a[0] >= 20.0 for a, b in zip(cues, cues[1:]))


def test_sfx_toggle():
    assert sfx_enabled("auto", True) and not sfx_enabled("auto", False)
    assert sfx_enabled("on", False) and not sfx_enabled("off", True)


def test_user_sfx_file_wins(tmp_path):
    (tmp_path / "sfx").mkdir()
    (tmp_path / "sfx" / "pop.wav").write_bytes(b"RIFF")
    assert sfx_path("pop", tmp_path) == tmp_path / "sfx" / "pop.wav"


def _clip(i: int) -> ClipInfo:
    return ClipInfo(path=Path(f"/tmp/clip_{i}.mp4"), scene_index=i, in_ts=i * 10.0,
                    out_ts=(i + 1) * 10.0, duration=10.0, has_audio=True, effects_applied=[])


def test_sfx_mix_into_the_final_bus_only():
    cfg = ComposeConfig(captions=CaptionStyle(mode="off"), effects=EffectsConfig(unsharp=False))
    cues = [(Path("/sfx/pop.wav"), 2.5), (Path("/sfx/whoosh.wav"), 9.65)]
    plan = build_final_command(clips=[_clip(0), _clip(1)], analysis=_graph_analysis(2),
                               music_path=Path("/tmp/m.wav"), captions_path=None, config=cfg,
                               output_path=Path("/tmp/o.mp4"), sfx=cues)
    fc = plan.filter_complex
    assert plan.args.count("-i") == 5  # 2 clips + music + 2 cues
    assert "[3:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=-14.0dB,adelay=2500|2500[sfx0]" in fc
    assert "[4:a]" in fc and "adelay=9650|9650[sfx1]" in fc
    assert "[amixed][sfx0][sfx1]amix=" in fc and "normalize=0" in fc
    # The loudnorm + limiter still close the bus, after the effects.
    assert fc.index("[asfx]") < fc.rindex("loudnorm=I=")
    chunk = build_final_command(clips=[_clip(0), _clip(1)], analysis=_graph_analysis(2),
                                music_path=None, captions_path=None, config=cfg,
                                output_path=Path("/tmp/o.mp4"), sfx=cues, final_pass=False)
    assert "sfx" not in chunk.filter_complex and chunk.args.count("-i") == 2


def test_synthesized_sfx_are_short_stereo_and_below_full_scale(tmp_path):
    script = Path("/app/assets/sfx/synthesize_sfx.sh")
    if not script.exists():
        script = Path(__file__).resolve().parents[2] / "assets" / "sfx" / "synthesize_sfx.sh"
    subprocess.run(["bash", str(script), str(tmp_path)], check=True, capture_output=True)
    for kind in ("pop", "hit", "whoosh"):
        f = tmp_path / f"{kind}.wav"
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate:format=duration",
             "-of", "json", str(f)], check=True, capture_output=True, text=True)
        info = json.loads(probe.stdout)
        assert info["streams"][0]["channels"] == 2 and info["streams"][0]["sample_rate"] == "48000"
        assert float(info["format"]["duration"]) <= 0.61
        vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(f), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        peak = float(vol.split("max_volume:")[1].split("dB")[0])
        assert -8.0 < peak < -1.0
