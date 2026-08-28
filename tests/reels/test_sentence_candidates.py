"""CP3: utterance-unit building + sentence-aligned candidate generation."""

from __future__ import annotations

from reelforge_core.models import (
    SelectionConfig,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from reelforge_core.reels import generate_candidates
from reelforge_core.reels.generators.sentence import (
    build_units,
    generate_sentence_candidates,
)

from tests.reels._fixtures import make_analysis


def _w(start: float, end: float, word: str) -> TranscriptWord:
    return TranscriptWord(start=start, end=end, word=word, probability=0.95)


def _transcript(segments: list[TranscriptSegment]) -> Transcript:
    return Transcript(
        language="en",
        language_probability=1.0,
        duration=segments[-1].end if segments else 0.0,
        segments=segments,
    )


def _seg(words: list[TranscriptWord]) -> TranscriptSegment:
    return TranscriptSegment(
        start=words[0].start,
        end=words[-1].end,
        text=" ".join(w.word for w in words),
        words=words,
    )


def _analysis_with_transcript(asset_id, scene_durs, transcript):
    analysis = make_analysis(asset_id, scene_durs)
    return analysis.model_copy(update={"transcript": transcript})


# ---- unit building ---------------------------------------------------------


def test_units_split_on_sentence_punctuation():
    words = [
        _w(0.0, 0.5, "Hello"),
        _w(0.6, 1.2, "world."),
        _w(1.3, 1.9, "Next"),
        _w(2.0, 2.6, "sentence."),
    ]
    units = build_units(_transcript([_seg(words)]))
    assert [(u.start, u.end) for u in units] == [(0.0, 1.2), (1.3, 2.6)]
    assert units[0].text == "Hello world."


def test_units_split_on_long_gap():
    # No punctuation; 0.5s gap after "pause" >= SENTENCE_GAP_SEC (0.45).
    words = [
        _w(0.0, 0.6, "before"),
        _w(0.7, 1.3, "pause"),
        _w(1.8, 2.4, "after"),
        _w(2.5, 3.1, "words"),
    ]
    units = build_units(_transcript([_seg(words)]))
    assert [(u.start, u.end) for u in units] == [(0.0, 1.3), (1.8, 3.1)]


def test_units_split_on_segment_boundary_with_small_gap():
    # Segment boundary + 0.3s gap (>= 0.25 but < 0.45): splits only because
    # of the segment change.
    seg_a = _seg([_w(0.0, 0.6, "first"), _w(0.7, 1.3, "segment")])
    seg_b = _seg([_w(1.6, 2.2, "second"), _w(2.3, 2.9, "segment")])
    units = build_units(_transcript([seg_a, seg_b]))
    assert [(u.start, u.end) for u in units] == [(0.0, 1.3), (1.6, 2.9)]
    # Same gap WITHOUT a segment boundary does not split.
    one_seg = _seg(
        [_w(0.0, 0.6, "first"), _w(0.7, 1.3, "segment"), _w(1.6, 2.2, "second"), _w(2.3, 2.9, "segment")]
    )
    assert len(build_units(_transcript([one_seg]))) == 1


def test_short_units_merge_into_successor():
    # "Yeah." is a 0.4s unit -> merges into the following sentence.
    words = [
        _w(0.0, 0.4, "Yeah."),
        _w(0.5, 1.1, "So"),
        _w(1.2, 1.8, "anyway"),
        _w(1.9, 2.5, "then."),
    ]
    units = build_units(_transcript([_seg(words)]))
    assert len(units) == 1
    assert units[0].start == 0.0 and units[0].end == 2.5
    assert units[0].text.startswith("Yeah. So")


def test_trailing_short_unit_folds_into_predecessor():
    words = [
        _w(0.0, 0.6, "A"),
        _w(0.7, 1.4, "sentence."),
        _w(1.5, 1.9, "Bye."),  # trailing 0.4s fragment
    ]
    units = build_units(_transcript([_seg(words)]))
    assert len(units) == 1
    assert units[0].end == 1.9


def test_units_empty_transcript():
    assert build_units(None) == []
    assert build_units(_transcript([])) == []


# ---- span enumeration ------------------------------------------------------


def _monologue(n_sentences: int = 12, sec_per_sentence: float = 10.0) -> Transcript:
    """n sentences, each `sec_per_sentence` long and densely worded."""
    segs = []
    for s in range(n_sentences):
        base = s * sec_per_sentence
        words = []
        n_words = 12
        step = sec_per_sentence / n_words
        for k in range(n_words):
            t = base + k * step
            text = f"w{s}_{k}" + ("." if k == n_words - 1 else "")
            words.append(_w(t, t + step * 0.8, text))
        segs.append(_seg(words))
    return _transcript(segs)


def test_monologue_spans_land_on_sentence_edges():
    transcript = _monologue()  # 12 sentences x 10s = 120s
    analysis = _analysis_with_transcript("mono", [120.0], transcript)
    units = build_units(transcript)
    assert len(units) == 12
    cands = generate_sentence_candidates(analysis, SelectionConfig())
    assert cands
    unit_starts = {u.start for u in units}
    unit_ends = {u.end for u in units}
    for c in cands:
        assert c.start_sec in unit_starts
        assert c.end_sec in unit_ends
        assert 30.0 <= c.duration_sec <= 60.0
        assert c.source == "sentence"
        assert c.scene_indices == [0]  # single 120s scene covers every span


def test_silent_source_yields_zero_sentence_candidates():
    analysis = make_analysis("silent", [40.0], with_audio=False)
    assert analysis.transcript is None
    assert generate_sentence_candidates(analysis, SelectionConfig()) == []
    # The union still returns scene candidates.
    cands = generate_candidates(analysis, SelectionConfig())
    assert cands and all(c.source == "scene" for c in cands)


def test_speech_ratio_guard_skips_sparse_speech():
    # Two 1.5s utterances 40s apart: any 30-60s span has < 15% spoken time.
    seg_a = _seg([_w(0.0, 0.7, "hey"), _w(0.8, 1.5, "there.")])
    seg_b = _seg([_w(40.0, 40.7, "still"), _w(40.8, 41.5, "here.")])
    analysis = _analysis_with_transcript("sparse", [60.0], _transcript([seg_a, seg_b]))
    assert generate_sentence_candidates(analysis, SelectionConfig()) == []


def test_union_dedups_exact_scene_sentence_collision():
    # One 40s scene whose speech starts exactly at the scene edges: the
    # sentence span (0, 40) collides with the scene span (0, 40).
    words = [_w(0.0, 19.0, "long"), _w(19.5, 40.0, "speech.")]
    analysis = _analysis_with_transcript("collide", [40.0], _transcript([_seg(words)]))
    # Two units needed for the generator: split via punctuation.
    assert len(build_units(analysis.transcript)) >= 1
    cands = generate_candidates(analysis, SelectionConfig())
    spans = [(c.start_sec, c.end_sec) for c in cands]
    assert spans.count((0.0, 40.0)) == 1
    winner = next(c for c in cands if (c.start_sec, c.end_sec) == (0.0, 40.0))
    # Sentence generator runs first, so it wins the collision.
    assert winner.source == "sentence"


def test_max_candidates_cap_keeps_sentence_first_and_strides_scene():
    transcript = _monologue(n_sentences=12)
    analysis = _analysis_with_transcript("cap", [2.0] * 60, transcript)
    cfg = SelectionConfig(max_candidates=30)
    cands = generate_candidates(analysis, cfg)
    assert len(cands) == 30
    n_sentence = sum(1 for c in cands if c.source == "sentence")
    n_scene = sum(1 for c in cands if c.source == "scene")
    # All sentence candidates fit under the cap and survive; scene fills the rest.
    full = generate_sentence_candidates(analysis, cfg)
    assert n_sentence == len(full)
    assert n_scene == 30 - n_sentence
    # Strided scene survivors stay time-ordered and span the asset.
    scene_kept = [c for c in cands if c.source == "scene"]
    starts = [c.start_sec for c in scene_kept]
    assert starts == sorted(starts)
