"""Reel selection: candidate generation → LLM ranking → dedup → ReelSelection."""

from reelforge_core.reels.candidates import generate_candidates
from reelforge_core.reels.dedup import dedup, overlap_ratio
from reelforge_core.reels.pipeline import select_reels

__all__ = ["dedup", "generate_candidates", "overlap_ratio", "select_reels"]
