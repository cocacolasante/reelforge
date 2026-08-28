"""Shared word-timeline helpers for selection (pure).

`flatten_words` is defined once in compose/speech_snap.py (where the
speech-safe cut logic lives); selection imports it from here so both sides
agree on what "the word timeline" means. The dependency edge is one-way
(reels → compose); compose never imports reels.
"""

from __future__ import annotations

from reelforge_core.compose.speech_snap import flatten_words

__all__ = ["flatten_words"]
