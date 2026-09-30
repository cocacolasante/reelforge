"""CP2 (pro-editing plan): restrained punch captions inside the platform-safe
zone, emoji stripped, keyword-only highlights, voice cleanup and edge fades."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.captions import build_captions, strip_emoji
from reelforge_core.compose.graph_builder import build_final_command, edge_fades, voice_chain
from reelforge_core.compose.keywords import pick_keywords
from reelforge_core.compose.safezone import safe_rect
from reelforge_core.compose.textfit import wrap_to_width
from reelforge_core.models import CaptionStyle, ComposeConfig, EffectsConfig, TextOverlay
from reelforge_core.qa.captions_geom import parse_ass
from reelforge_core.qa.metrics import caption_stats
from reelforge_core.models import TranscriptWord
from tests.compose.test_captions import _analysis_with_transcript, _reel, _scene

# A realistic run of speech: long words, a number, a pause, a question.
SPEECH = (
    "So the first thing you want to do is let the board sit in the sun for "
    "about 20 minutes. Then grab a plastic scraper, never a metal one, and "
    "work from the nose toward the tail. Why does that matter? Because metal "
    "gouges the fiberglass and the repair costs $400."
).split()


# --- emoji ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,clean",
    [("From practice to perfection 🛹", "From practice to perfection"),
     ("🔥🔥 Big air", "Big air"), ("no emoji here", "no emoji here"), ("👍🏽", "")],
)
def test_strip_emoji(raw, clean):
    assert strip_emoji(raw) == clean


# --- keywords -------------------------------------------------------------------


def test_keywords_pick_numbers_and_strong_words_one_per_chunk():
    chunks = [["I", "paid", "$400"], ["for", "the"], ["worst", "board"], ["ever", "made."]]
    lit = pick_keywords(chunks)
    assert (0, 2) in lit and (2, 0) in lit
    assert len({c for c, _ in lit}) == len(lit)  # at most one per chunk


def test_keywords_stay_within_the_share_band():
    words = "we went down to the beach and then we paddled out to the break".split()
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    share = len(pick_keywords(chunks)) / len(words)
    assert 0.10 <= share <= 0.25


# --- text fitting ---------------------------------------------------------------


def test_wrap_never_exceeds_the_width():
    lines = wrap_to_width("the quick brown fox jumps over the lazy dog".split(), "Inter", 86, 400)
    assert len(lines) > 1
    assert all(len(ln) for ln in lines)


# --- captions inside the safe zone ------------------------------------------------


def _speech_words() -> list[TranscriptWord]:
    words, t = [], 0.2
    for w in SPEECH:
        words.append(TranscriptWord(start=t, end=t + 0.28, word=w, probability=0.9))
        t += 0.62 if w.endswith((".", "?")) else 0.32
    return words


def _render(
    tmp_path: Path, mode: str, position: str = "lower_third", overlays=None, **style
) -> Path:
    scenes = [_scene(0, 0.0, 30.0)]
    analysis = _analysis_with_transcript(_speech_words(), scenes)
    cfg = ComposeConfig(captions=CaptionStyle(mode=mode, position=position, **style))
    return build_captions(_reel([0], 0.0, 30.0), analysis, cfg, tmp_path, overlays=overlays)


@pytest.mark.parametrize("mode", ["punch", "static", "karaoke"])
@pytest.mark.parametrize("position", ["lower_third", "centered", "top"])
def test_every_mode_and_position_stays_inside_the_safe_zone(tmp_path, mode, position):
    path = _render(tmp_path, mode, position)
    doc = parse_ass(path)
    stats = caption_stats(doc.boxes, safe_rect(doc.width, doc.height))
    assert stats["safe_zone_violations"] == 0, stats["violations"]


@pytest.mark.parametrize("mode", ["static", "karaoke"])
def test_saved_inter_configs_stay_inside_the_safe_zone(tmp_path, mode):
    # Pre-CP2 reels saved 64px Inter. Inter gets ASS bold, so the packer
    # must measure the bold face — measuring regular let "flat for you to
    # scrape with," overflow the skimboard mix by 12px a side.
    path = _render(tmp_path, mode, font_family="Inter", font_size_px=64, outline_width_px=4)
    doc = parse_ass(path)
    stats = caption_stats(doc.boxes, safe_rect(doc.width, doc.height))
    assert stats["safe_zone_violations"] == 0, stats["violations"]


def test_punch_captions_are_short_and_highlight_sparingly(tmp_path):
    doc = parse_ass(_render(tmp_path, "punch"))
    stats = caption_stats(doc.boxes, safe_rect(doc.width, doc.height))
    assert stats["words_per_caption_max"] <= 3
    assert 0.0 < stats["highlighted_share"] <= 0.25
    text = Path(tmp_path / "captions.ass").read_text()
    assert "Montserrat Black" in text and "\\fscx108" in text  # heavy face, pop-in


def test_long_overlay_is_wrapped_and_emoji_free(tmp_path):
    hook = TextOverlay(id="h", text="Skater lines up the perfect trick at the park 🛹",
                       start_sec=0.4, end_sec=2.8, position="top", font_size_px=84)
    doc = parse_ass(_render(tmp_path, "off", overlays=[hook]))
    overlay = [b for b in doc.boxes if b.style == "Overlay"][0]
    assert "🛹" not in overlay.text and overlay.lines >= 2
    assert safe_rect(1080, 1920).contains(overlay.rect, tolerance=2.0)


# --- audio ----------------------------------------------------------------------------


def test_voice_chain_denoises_only_when_asked():
    on = voice_chain(EffectsConfig(voice_denoise="on"))
    off = voice_chain(EffectsConfig(voice_denoise="off"))
    assert on.startswith("highpass=f=75") and "afftdn" in on and "deesser" in on
    assert "afftdn" not in off and "acompressor" in off
    assert voice_chain(EffectsConfig(voice_enhance=False)) is None


def test_edge_fades_cover_both_ends():
    assert edge_fades(20.0) == "afade=t=in:d=0.03,afade=t=out:st=19.970:d=0.03"


def _clip(i, dur):
    from reelforge_core.compose.clips import ClipInfo

    return ClipInfo(path=Path(f"/tmp/c{i}.mp4"), scene_index=0, in_ts=0.0, out_ts=dur,
                    duration=dur, has_audio=True, effects_applied=[])


def test_chunk_passes_get_no_voice_chain_or_fades():
    """Per chunk, the chain would stack twice and fades would dip every join."""
    from tests.compose.test_graph_builder import _analysis

    common = dict(clips=[_clip(0, 10.0), _clip(1, 10.0)], analysis=_analysis(2), music_path=None,
                  captions_path=None, output_path=Path("/tmp/x.mp4"),
                  config=ComposeConfig(normalize_loudness=False,
                                       effects=EffectsConfig(unsharp=False, ken_burns_on_low_energy=False)))
    final = build_final_command(**common).filter_complex
    chunk = build_final_command(**common, final_pass=False).filter_complex
    assert "highpass" in final and "afade" in final
    assert "highpass" not in chunk and "afade" not in chunk
