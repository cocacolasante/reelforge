"""Sentence-aligned candidate generator (pure).

Speech footage rarely cuts where PySceneDetect cuts. This generator builds
*utterance units* from the word timeline and enumerates spans that start on a
unit start and end on a unit end, so candidates open and close on natural
speech boundaries instead of visual ones.

Unit boundaries (after a word) are any of:
  (a) the word ends with sentence-final punctuation (. ? !),
  (b) the gap to the next word is >= SENTENCE_GAP_SEC,
  (c) a transcript segment boundary that coincides with a gap >= SEGMENT_GAP_SEC.
Units shorter than MIN_UNIT_SEC merge into their successor (a trailing short
unit folds into its predecessor).
"""

from __future__ import annotations

from dataclasses import dataclass

from reelforge_core.models import (
    AnalysisReport,
    ReelCandidate,
    SelectionConfig,
    Transcript,
)

SENTENCE_GAP_SEC = 0.45
SEGMENT_GAP_SEC = 0.25
MIN_UNIT_SEC = 1.0
# Below this spoken-time fraction a span is B-roll with stray words — the
# scene (and later moment) generators already cover it.
MIN_SPEECH_RATIO = 0.15
_SENTENCE_FINAL = (".", "?", "!")


@dataclass(frozen=True)
class UtteranceUnit:
    start: float
    end: float
    text: str
    spoken_sec: float  # sum of word durations inside the unit


def build_units(transcript: Transcript | None) -> list[UtteranceUnit]:
    """Split the word timeline into utterance units. Pure."""
    if transcript is None:
        return []
    flat: list[tuple[object, int]] = []  # (TranscriptWord, segment_index)
    for si, seg in enumerate(transcript.segments):
        for w in seg.words:
            flat.append((w, si))
    if not flat:
        return []

    raw: list[UtteranceUnit] = []
    cur_words: list = []

    def _flush() -> None:
        if not cur_words:
            return
        raw.append(
            UtteranceUnit(
                start=cur_words[0].start,
                end=cur_words[-1].end,
                text=" ".join(w.word.strip() for w in cur_words),
                spoken_sec=sum(w.end - w.start for w in cur_words),
            )
        )
        cur_words.clear()

    for k, (w, si) in enumerate(flat):
        cur_words.append(w)
        if k + 1 >= len(flat):
            break
        nxt, nsi = flat[k + 1]
        gap = nxt.start - w.end
        if (
            w.word.rstrip().endswith(_SENTENCE_FINAL)
            or gap >= SENTENCE_GAP_SEC
            or (nsi != si and gap >= SEGMENT_GAP_SEC)
        ):
            _flush()
    _flush()

    # Merge sub-second fragments into their successor so a stray "Yeah." can't
    # become a span boundary on its own.
    merged: list[UtteranceUnit] = []
    pending: UtteranceUnit | None = None
    for u in raw:
        if pending is not None:
            u = _combine(pending, u)
            pending = None
        if u.end - u.start < MIN_UNIT_SEC:
            pending = u
            continue
        merged.append(u)
    if pending is not None:
        if merged:
            merged[-1] = _combine(merged[-1], pending)
        else:
            merged.append(pending)
    return merged


def _combine(a: UtteranceUnit, b: UtteranceUnit) -> UtteranceUnit:
    return UtteranceUnit(
        start=a.start,
        end=b.end,
        text=(a.text + " " + b.text).strip(),
        spoken_sec=a.spoken_sec + b.spoken_sec,
    )


def generate_sentence_candidates(
    analysis: AnalysisReport, config: SelectionConfig
) -> list[ReelCandidate]:
    """Spans that start on a unit start and end on a unit end, within the
    duration window. Pure; no count cap (the union cap handles volume)."""
    from reelforge_core.reels.candidates import _candidate_id, covering_scenes

    units = build_units(analysis.transcript)
    if len(units) < 2:
        return []
    min_sec = config.effective_min_sec
    max_sec = config.effective_max_sec
    # Prefix sums of spoken time -> O(1) speech-ratio checks per span.
    prefix = [0.0]
    for u in units:
        prefix.append(prefix[-1] + u.spoken_sec)

    out: list[ReelCandidate] = []
    n = len(units)
    for i in range(n):
        for j in range(i, n):
            start = units[i].start
            end = units[j].end
            dur = end - start
            if dur > max_sec:
                break  # unit ends are monotonic; extending only lengthens
            if dur < min_sec:
                continue
            spoken = prefix[j + 1] - prefix[i]
            if dur > 0 and spoken / dur < MIN_SPEECH_RATIO:
                continue
            covered = covering_scenes(analysis.scenes, start, end)
            out.append(
                ReelCandidate(
                    candidate_id=_candidate_id(analysis.asset_id, start, end),
                    scene_indices=covered,
                    start_sec=start,
                    end_sec=end,
                    duration_sec=round(dur, 6),
                    scene_count=len(covered),
                    source="sentence",
                )
            )
    return out
