"""analyze --resume must keep the transcript.

transcript.json has two on-disk shapes: transcribe() writes a bare Transcript
dump when there is speech and {"transcript": null} when there isn't (voiceover
takes use {"transcript": {...}}). The resume path used to return None for every
bare dump, so re-analyzing with resume silently stripped all speech from
analysis.json (and re-ran semantics against empty transcript slices).
"""

from __future__ import annotations

import json
from pathlib import Path

from reelforge_core.analysis.pipeline import _load_transcript_json
from reelforge_core.models import Transcript


def _transcript() -> Transcript:
    return Transcript.model_validate(
        {
            "language": "en",
            "language_probability": 0.99,
            "duration": 4.0,
            "segments": [
                {
                    "start": 0.5,
                    "end": 1.4,
                    "text": " Here it comes.",
                    "words": [
                        {"start": 0.5, "end": 0.8, "word": " Here", "probability": 0.9},
                        {"start": 0.8, "end": 1.0, "word": " it", "probability": 0.9},
                        {"start": 1.0, "end": 1.4, "word": " comes.", "probability": 0.9},
                    ],
                }
            ],
        }
    )


def test_bare_dump_from_transcribe_loads(tmp_path: Path) -> None:
    """The shape transcribe() writes for any clip with speech."""
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps(_transcript().model_dump()))
    assert _load_transcript_json(p) == _transcript()


def test_null_wrapper_for_silent_sources_loads_as_none(tmp_path: Path) -> None:
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps({"transcript": None}))
    assert _load_transcript_json(p) is None


def test_wrapped_transcript_loads(tmp_path: Path) -> None:
    """The voiceover-take cache shape (transcribe.ensure_take_transcript)."""
    p = tmp_path / "transcript.json"
    p.write_text(json.dumps({"transcript": _transcript().model_dump()}))
    assert _load_transcript_json(p) == _transcript()
