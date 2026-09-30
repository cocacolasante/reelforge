"""CP1 (pro-editing plan): the ranker is told the real span length, sees the
whole transcript, and only gets sampling params models accept."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from reelforge_core.models import SelectionConfig
from reelforge_core.reels import rank
from reelforge_core.reels.candidates import generate_candidates
from tests.reels.test_select_pipeline import make_analysis


def test_prompt_states_the_duration_actually_requested():
    """v4 said "30-60 second" for every request."""
    short = rank.build_system_prompt(SelectionConfig(target_min_sec=15, target_max_sec=30))
    assert "15-30 second standalone short-form video" in short
    assert "30-60" not in short
    default = rank.build_system_prompt(SelectionConfig())
    assert "30-60 second standalone short-form video" in default


def test_long_spans_are_framed_as_sections_of_a_long_video():
    long_cfg = SelectionConfig(output_form="long_single", long_target_duration_sec=300)
    prompt = rank.build_system_prompt(long_cfg)
    assert "section of a longer YouTube video" in prompt
    assert "TikTok" not in prompt.split("\n\n")[0]


def test_prompt_template_renders_with_no_leftover_placeholders():
    for cfg in (SelectionConfig(), SelectionConfig(prompt="the fails")):
        text = rank.build_system_prompt(cfg)
        assert "{" not in text.split("USER DIRECTION")[0]


def _words_analysis(n_words: int):
    """One 60s scene with n_words evenly spoken across it."""
    from reelforge_core.models import Transcript, TranscriptSegment, TranscriptWord

    analysis = make_analysis("cp1w", [60.0])
    step = 60.0 / n_words
    words = [
        TranscriptWord(word=f"w{i}", start=i * step, end=i * step + step * 0.8, probability=0.9)
        for i in range(n_words)
    ]
    seg = TranscriptSegment(id=0, start=0.0, end=60.0, text=" ".join(w.word for w in words), words=words)
    return analysis.model_copy(
        update={"transcript": Transcript(language="en", language_probability=1.0, duration=60.0, segments=[seg])}
    )


def test_short_spans_send_every_word_with_timestamps():
    analysis = _words_analysis(100)
    cand = generate_candidates(analysis, SelectionConfig(target_min_sec=20, target_max_sec=60))[0]
    ctx = rank.build_candidate_context(cand, analysis)
    assert "transcript_middle" not in ctx
    assert all(isinstance(w, list) for w in ctx["transcript_words"])


def test_long_spans_send_edges_timestamped_and_the_middle_as_text():
    """v4 kept only 60 words at each end, so the middle — where the story
    develops — was invisible to the ranker."""
    analysis = _words_analysis(400)
    cand = max(
        generate_candidates(analysis, SelectionConfig(target_min_sec=20, target_max_sec=60)),
        key=lambda c: c.duration_sec,
    )
    ctx = rank.build_candidate_context(cand, analysis)
    stamped = [w for w in ctx["transcript_words"] if isinstance(w, list)]
    assert len(stamped) == 2 * rank.EDGE_WORDS
    assert "…" in ctx["transcript_words"]
    middle = ctx["transcript_middle"].split()
    assert 0 < len(middle) <= rank.MIDDLE_WORDS + 1  # + the "[…]" marker
    # The middle words really are the ones between the two edges.
    first_middle = stamped[rank.EDGE_WORDS - 1][1]
    assert middle[0] == f"w{int(first_middle[1:]) + 1}"


class _Client:
    def __init__(self):
        self.kwargs = None
        self.messages = self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason="tool_use", content=[], usage=None)


@pytest.mark.parametrize(
    "model,expect_temperature",
    [("claude-sonnet-4-5", True), ("claude-haiku-4-5-20251001", True),
     ("claude-opus-5-5", False), ("claude-sonnet-5", False)],
)
def test_temperature_only_goes_to_models_known_to_accept_it(model, expect_temperature):
    client = _Client()
    asyncio.run(
        rank._call_model(client, model=model, temperature=0.0, system_prompt="s", messages=[])
    )
    assert ("extra_body" in client.kwargs) is expect_temperature
    if expect_temperature:
        assert client.kwargs["extra_body"] == {"temperature": 0.0}
