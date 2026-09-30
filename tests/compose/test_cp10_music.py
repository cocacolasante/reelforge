"""Pro-editing CP10: whole-track music analysis, section choice (the drop on
the payoff, endings on a phrase), phrase-aligned loops, BPM-aware picks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from reelforge_core.compose.music import build_music_prep_command, select_track
from reelforge_core.compose.music_analysis import (
    LOOP_XFADE_SEC,
    TrackAnalysis,
    analyze_samples,
    analyze_track,
    bpm_fits,
    choose_section,
    grid_for_offset,
    loop_plan,
    refine_offset,
)
from reelforge_core.models import ComposeConfig, MusicTrack
from tests.compose.test_speech_snap import _reel

SR = 22050


def _clicks(bpm=120.0, seconds=64.0, accent_every=4, loud_from=None, first=0.25):
    """A click track: a click per beat, the bar's first beat accented; after
    `loud_from` seconds everything is 3x louder (a drop)."""
    n = int(seconds * SR)
    y = np.zeros(n, dtype=np.float32)
    interval = 60.0 / bpm
    k = 0
    t = first
    while t < seconds - 0.05:
        amp = 0.9 if k % accent_every == 0 else 0.3
        if loud_from is not None and t < loud_from:
            amp /= 3.0
        i = int(t * SR)
        y[i:i + 400] += amp * np.hanning(400).astype(np.float32)
        k += 1
        t += interval
    return y


def _ta(**kw) -> TrackAnalysis:
    base = dict(bpm=120.0, phase_sec=0.25, downbeat_sec=0.25, duration_sec=128.0, phrase_sec=16.0,
                phrase_starts=[0.25 + 16.0 * i for i in range(8)],
                phrase_energy=[0.3, 0.3, 1.0, 1.0, 0.4, 1.0, 1.0, 0.5], drop_sec=32.25)
    base.update(kw)
    return TrackAnalysis(**base)


# ---- analysis ----------------------------------------------------------------------


def test_analysis_finds_downbeat_phrases_and_the_drop():
    y = _clicks(loud_from=32.25, first=0.25)
    ta = analyze_samples(y, SR, bpm=120.0, phase=0.25)
    assert ta.downbeat_sec == pytest.approx(0.25)  # the accented beat is "one"
    assert ta.phrase_sec == pytest.approx(16.0)  # 8 bars of 4 at 120 BPM
    assert ta.phrase_starts[:3] == pytest.approx([0.25, 16.25, 32.25])
    assert ta.drop_sec == pytest.approx(32.25)


def test_downbeat_is_the_accented_beat_not_the_first():
    y = _clicks(first=0.25)
    # Claim the grid starts one beat earlier: the accent still decides "one".
    ta = analyze_samples(y, SR, bpm=120.0, phase=0.25 - 0.5 + 60 / 120.0)
    assert (ta.downbeat_sec - 0.25) % 2.0 == pytest.approx(0.0, abs=1e-3)


def test_real_decode_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("REELFORGE_MUSIC_DIR", str(tmp_path / "music"))
    wav = tmp_path / "click.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "aevalsrc='if(lt(mod(t,0.5),0.02),sin(2*PI*1000*t)*if(lt(t,32),0.3,0.9),0)':s=44100:d=64",
         str(wav)],
        check=True, capture_output=True,
    )
    ta = analyze_track(wav)
    assert ta is not None and ta.bpm == pytest.approx(120.0, abs=1.5)
    assert ta.drop_sec is not None and abs(ta.drop_sec - 32.0) < 2.1
    assert list((tmp_path / "music" / "analysis").glob("*.json"))
    assert analyze_track(tmp_path / "missing.wav") is None


# ---- section choice ------------------------------------------------------------------


def test_drop_lands_on_the_payoff_on_a_whole_beat():
    ta = _ta()
    off, why = choose_section(ta, reel_sec=30.0, drop_at=12.3)
    assert why.startswith("drop")
    # The drop plays at mezzanine drop_sec - off, within half a beat of 12.3...
    assert abs((ta.drop_sec - off) - 12.3) <= ta.interval / 2 + 1e-6
    # ...and the offset is a whole beat on the track's grid.
    assert ((off - ta.phase_sec) / ta.interval) == pytest.approx(round((off - ta.phase_sec) / ta.interval))


def test_without_a_drop_the_reel_ends_on_a_phrase():
    ta = _ta(drop_sec=None)
    off, why = choose_section(ta, reel_sec=30.0)
    assert why.startswith("ends on the phrase")
    assert any(abs(off + 30.0 - p) < ta.interval for p in ta.phrase_starts)


def test_no_analysis_or_nothing_fits_is_the_top():
    assert choose_section(None, 30.0) == (0.0, "no beat analysis")
    # Nothing fits: from the FIRST BEAT, so the offset is still a whole beat.
    assert choose_section(_ta(duration_sec=20.0, phrase_starts=[0.25, 16.25]), 60.0)[0] == 0.25


def test_every_section_puts_beats_at_phase_zero():
    from reelforge_core.compose.music_analysis import mezzanine_grid

    ta = _ta()
    for args in ((30.0, 12.3), (30.0, None), (500.0, None)):
        off, _ = choose_section(ta, *args)
        g = grid_for_offset(ta, off)
        assert min(g.phase_sec, ta.interval - g.phase_sec) < 1e-3
    assert mezzanine_grid(ta).phase_sec == 0.0 and mezzanine_grid(ta).bpm == ta.bpm


def test_grid_for_offset_keeps_the_music_beats():
    ta = _ta()
    grid = grid_for_offset(ta, 30.25)
    # A beat of the track at 31.25 plays at mezzanine 1.0.
    assert grid.snap(1.0) == pytest.approx(1.0)
    assert grid.phase_sec == pytest.approx(0.0)


def test_refine_moves_by_whole_beats_only():
    ta = _ta()
    assert refine_offset(ta, 20.25, planned_at=12.0, actual_at=13.1) == pytest.approx(19.25)
    assert refine_offset(ta, 20.25, planned_at=12.0, actual_at=12.2) == pytest.approx(20.25)
    assert refine_offset(ta, 0.25, planned_at=0.0, actual_at=5.0) == 0.25  # would go negative


def test_loop_plan_is_phrase_aligned_and_covers_the_reel():
    ta = _ta()
    assert loop_plan(ta, 10.0, 60.0) == [(10.0, 70.0)]
    segs = loop_plan(ta, 20.25, 400.0)
    assert segs[0][0] == 20.25 and segs[0][1] == ta.phrase_starts[-1]
    assert all(a == ta.phrase_starts[1] for a, _ in segs[1:])
    covered = sum(b - a for a, b in segs) - LOOP_XFADE_SEC * (len(segs) - 1)
    assert covered >= 400.0 - 1e-6


def test_bpm_band_counts_half_and_double_time():
    assert bpm_fits(128) and bpm_fits(64) and bpm_fits(250)
    assert not bpm_fits(95) and not bpm_fits(None)


# ---- prep + selection ------------------------------------------------------------------


def _track(tid, bpm, mood="energetic", source="user") -> MusicTrack:
    return MusicTrack(id=tid, path=f"/m/{tid}.mp3", source=source, bpm=bpm, mood=mood,
                      duration_sec=180.0, license="CC0")


def test_sectioned_prep_crossfades_segments_and_fades_on_the_phrase():
    cmd = build_music_prep_command(track=_track("t", 120), out_path=Path("/o.wav"),
                                   target_duration_sec=90.0, config=ComposeConfig(),
                                   segments=[(20.25, 112.25), (16.25, 40.0)], fade_out_sec=1.5)
    assert cmd.count("-i") == 2 and "20.250" in cmd and "16.250" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "acrossfade=d=2.00" in fc and "afade=t=out:st=88.500:d=1.50" in fc
    assert "-stream_loop" not in cmd
    legacy = build_music_prep_command(track=_track("t", 120), out_path=Path("/o.wav"),
                                      target_duration_sec=30.0, config=ComposeConfig())
    assert "-stream_loop" in legacy  # no analysis: the old path, untouched


def test_hype_prefers_a_driving_tempo():
    lib = [_track("slow1", 84), _track("slow2", 90), _track("fast", 128)]
    reel = _reel([0], 0.0, 30.0).model_copy(update={"suggested_mood": "energetic"})
    assert select_track(lib, ComposeConfig(), reel, style="hype").id == "fast"
    # Other styles pick among every mood match as before.
    picks = {select_track(lib, ComposeConfig(seed=s), reel).id for s in range(12)}
    assert picks > {"fast"}


def test_realign_end_lands_on_a_phrase_by_whole_beats():
    from reelforge_core.compose.music_analysis import realign_end

    ta = _ta(drop_sec=None)
    # Planned 21.5s ending on the 32.25 phrase from 10.75; the render came out
    # at 20.3s: shift so it ends within half a beat of a phrase.
    off = realign_end(ta, 10.75, 20.3)
    assert min(abs(off + 20.3 - p) for p in ta.phrase_starts) <= ta.interval / 2 + 1e-6
    assert ((off - 10.75) / ta.interval) == pytest.approx(round((off - 10.75) / ta.interval))


def test_a_later_smaller_drop_is_used_when_the_big_one_is_too_early():
    ta = _ta(drop_sec=32.25, drops=[32.25, 80.25])
    off, why = choose_section(ta, reel_sec=45.0, drop_at=40.0)
    assert why == "drop at 80.2s on the payoff" or why.startswith("drop at 80.")
    assert abs((80.25 - off) - 40.0) <= ta.interval / 2 + 1e-6
