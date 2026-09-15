"""Audio-measured silence: the speech envelope, audio-driven jump cuts,
speech-safe beat-trim caps, and the talking-head no-nudge rule."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from reelforge_core.compose.beats import BeatGrid, compute_beat_end_trims
from reelforge_core.compose.director import apply_director
from reelforge_core.compose.jumpcuts import JUMP_CUT, apply_jump_cuts, split_on_silences
from reelforge_core.compose.silence import (
    SpeechEnvelope,
    envelope_from_samples,
    load_speech_envelope,
    speech_free_tail,
)
from reelforge_core.mixes.planner import plan_mix
from tests.compose.test_director import _an, _plan
from tests.compose.test_jumpcuts import _transcript, _w


def _env(loud: list[tuple[float, float]], duration: float) -> SpeechEnvelope:
    """-60 dB everywhere, -20 dB inside the `loud` spans; threshold -40."""
    hop = 0.02
    db = np.full(int(round(duration / hop)), -60.0)
    for s, e in loud:
        db[int(round(s / hop)) : int(round(e / hop))] = -20.0
    return SpeechEnvelope(db=db, threshold_db=-40.0, hop_sec=hop)


# ---- envelope ----------------------------------------------------------------


def _samples(sr: int = 16000) -> np.ndarray:
    rng = np.random.default_rng(0)
    quiet = rng.normal(0, 0.001, sr)
    t = np.arange(sr) / sr
    tone = 0.3 * np.sin(2 * np.pi * 220 * t)
    return np.concatenate([quiet, tone, rng.normal(0, 0.001, sr)])


def test_envelope_finds_silence_around_a_tone():
    env = envelope_from_samples(_samples(), 16000)
    assert env is not None
    runs = env.silent_runs(0.0, 3.0, 0.45)
    assert len(runs) == 2
    assert runs[0] == pytest.approx((0.0, 1.0), abs=0.03)
    assert runs[1] == pytest.approx((2.0, 3.0), abs=0.03)


def test_load_speech_envelope_roundtrip_and_missing(tmp_path: Path):
    path = tmp_path / "audio.wav"
    pcm = (np.clip(_samples(), -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm.tobytes())
    env = load_speech_envelope(path)
    assert env is not None and env.duration == pytest.approx(3.0, abs=0.03)
    assert load_speech_envelope(tmp_path / "nope.wav") is None


def test_speech_free_tail_envelope_transcript_and_unknown():
    env = _env([(0.0, 2.0)], 3.0)
    assert speech_free_tail(0.0, 3.0, env) == pytest.approx(0.95)
    # Transcript fallback: trim may reach 0.4s past the last word's end stamp.
    t = _transcript([_w(0.5, 2.0)])
    assert speech_free_tail(0.0, 3.0, None, t) == pytest.approx(0.6)
    assert speech_free_tail(2.5, 3.0, None, t) == pytest.approx(0.5)  # no words in range
    assert speech_free_tail(0.0, 3.0) is None


# ---- audio-driven jump cuts ----------------------------------------------------


def test_audio_split_removes_measured_silence_with_small_pads():
    env = _env([(0.0, 3.0), (4.0, 7.0)], 7.0)
    assert split_on_silences((0.0, 7.0), None, envelope=env) == [(0.0, 3.1), (3.9, 7.0)]


def test_audio_split_ignores_short_pauses():
    env = _env([(0.0, 3.0), (3.3, 7.0)], 7.0)
    assert split_on_silences((0.0, 7.0), None, envelope=env) == [(0.0, 7.0)]


def test_audio_split_keeps_quiet_speech_the_transcript_heard():
    env = _env([(0.0, 3.0), (4.0, 7.0)], 7.0)
    t = _transcript([_w(0.5, 2.9), _w(3.2, 3.6), _w(4.1, 6.5)])
    assert split_on_silences((0.0, 7.0), t, envelope=env) == [(0.0, 7.0)]


def test_audio_split_trims_edge_dead_air_only_when_asked():
    env = _env([(1.0, 5.0)], 6.0)
    assert split_on_silences((0.0, 6.0), None, envelope=env) == [(0.0, 6.0)]
    assert split_on_silences((0.0, 6.0), None, envelope=env, trim_edges=True) == [(0.9, 5.1)]


def test_audio_split_refuses_fragments():
    env = _env([(0.0, 0.2), (1.2, 5.0)], 5.0)
    assert split_on_silences((0.0, 5.0), None, envelope=env) == [(0.0, 5.0)]


def test_apply_jump_cuts_merges_contiguous_scenes_for_straddling_pauses():
    env = _env([(0.0, 3.5), (4.5, 8.0)], 8.0)
    shots, per_cut = apply_jump_cuts([(0, 0.0, 4.0), (1, 4.0, 8.0)], None, envelope=env)
    assert shots == [(0, 0.0, 3.6), (1, 4.4, 8.0)]
    assert per_cut == [JUMP_CUT]


def test_apply_jump_cuts_keeps_separate_spans_apart():
    env = _env([(0.0, 10.0)], 10.0)
    shots, per_cut = apply_jump_cuts([(0, 0.0, 4.0), (2, 6.0, 10.0)], None, envelope=env)
    assert shots == [(0, 0.0, 4.0), (2, 6.0, 10.0)]
    assert per_cut == [None]


def test_mix_talking_head_uses_envelopes():
    env = _env([(0.0, 3.0), (4.0, 7.0)], 7.0)
    tl = plan_mix([("a", 0.0, 7.0)], {"a": None}, "talking_head", None, {"a": env})
    assert [(s.in_ts, s.out_ts) for s in tl.shots] == [(0.0, 3.1), (3.9, 7.0)]


# ---- beat trims + director -------------------------------------------------------


def test_beat_trims_respect_per_clip_speech_caps():
    g = BeatGrid(bpm=120.0, phase_sec=0.0)
    # Clip 0 would trim 0.3s but only 0.1s of its tail is silent -> untouched;
    # transition 1 then sits 0.4s past a beat (uncapped) -> trimmed.
    trims = compute_beat_end_trims([10.0, 10.0, 10.0], 0.4, g, max_trim=0.45, max_trims=[0.1, None])
    assert trims[0] == 0.0
    assert trims[1] == pytest.approx(0.4)


def test_director_cannot_nudge_talking_head_cuts():
    raw = {
        "shots": [{"index": 1, "nudge_start_sec": -0.5, "nudge_end_sec": 1.0, "reason": "x"}],
        "cuts": [],
        "hook_text": None,
    }
    new_plan, _, _ = apply_director(_plan("talking_head"), raw, _an())
    assert (new_plan.shots[1].in_ts, new_plan.shots[1].out_ts) == (3.0, 6.0)
