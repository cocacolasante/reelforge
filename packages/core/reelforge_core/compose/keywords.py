"""Which spoken words get the caption highlight — a deterministic heuristic.

Restrained captions highlight the words that carry the point (a number, a
superlative, a "never", a product name), not every word in turn. The target
is 10-25% of words, at most one per caption chunk. CP4's AI emphasis pass
replaces this; the heuristic stays as its fallback.
"""

from __future__ import annotations

import math
import re

MAX_SHARE = 0.25
MIN_SHARE = 0.10

_NUMBER = re.compile(r"\d|%|\$|€|£")
_STRONG = {
    # superlatives, absolutes and negations — words people stress
    "best", "worst", "most", "least", "biggest", "fastest", "easiest", "hardest",
    "never", "always", "only", "every", "everything", "nothing", "nobody",
    "first", "last", "secret", "free", "mistake", "mistakes", "wrong", "stop",
    "don't", "dont", "can't", "cant", "won't", "no", "not", "huge",
    "insane", "crazy", "perfect", "exactly", "literally", "why",
    "two", "three", "million", "thousand", "hundred",
}
_STOP = {
    "the", "a", "an", "and", "or", "but", "so", "to", "of", "in", "on", "at",
    "for", "with", "is", "it", "that", "this", "i", "you", "we", "they", "he",
    "she", "was", "are", "be", "been", "have", "has", "had", "do", "does", "did",
    "just", "like", "gonna", "going", "get", "got", "there", "here", "then",
    "your", "my", "our", "their", "its", "it's", "what", "when", "if", "about",
    "up", "out", "some", "really", "very", "can", "will", "would", "could",
}


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9'$%€£]", "", word.lower())


def _score(word: str, *, sentence_start: bool) -> float:
    w = _norm(word)
    if not w or w in _STOP:
        return 0.0
    if _NUMBER.search(word):
        return 3.0
    if w in _STRONG:
        return 2.5
    if w.endswith("est") and len(w) >= 6:
        return 2.0
    stripped = word.strip("\"'(“”‘’")
    if stripped[:1].isupper() and not sentence_start and len(w) >= 3:
        return 1.8  # a name mid-sentence: a product, place or person
    return min(1.0, len(w) / 10.0)  # longer content words carry more


def pick_keywords(
    chunks: list[list[str]], preferred: list[list[bool]] | None = None
) -> set[tuple[int, int]]:
    """(chunk index, word index) pairs to highlight: at most one per chunk,
    strongest first, between MIN_SHARE and MAX_SHARE of all words.

    `preferred` (same shape as `chunks`) marks the AI's key words
    (compose/emphasis.py). When given, those are the picks — one per chunk,
    up to the cap — and the heuristic only tops up to the floor; its own
    strong-word list no longer adds highlights the AI chose not to make."""
    total = sum(len(c) for c in chunks)
    if total == 0:
        return set()
    if preferred is not None:
        return _pick_preferred(chunks, preferred, total)
    best: list[tuple[float, int, int]] = []
    prev_end_of_sentence = True
    for ci, chunk in enumerate(chunks):
        scored = []
        for wi, word in enumerate(chunk):
            scored.append((_score(word, sentence_start=prev_end_of_sentence), wi))
            prev_end_of_sentence = word.rstrip().endswith((".", "!", "?"))
        top = max(scored)
        if top[0] > 0:
            best.append((top[0], ci, top[1]))
    best.sort(reverse=True)
    cap = max(1, int(total * MAX_SHARE))
    floor = max(1, math.ceil(total * MIN_SHARE))  # round() let 14 words get 7%
    # Strong words (>= 1.8) always qualify up to the cap; weaker content
    # words only top it up to the floor, so a plain sentence isn't painted.
    chosen = [b for b in best if b[0] >= 1.8][:cap]
    for b in best:
        if len(chosen) >= floor:
            break
        if b not in chosen:
            chosen.append(b)
    return {(ci, wi) for _, ci, wi in chosen[:cap]}


def _pick_preferred(
    chunks: list[list[str]], preferred: list[list[bool]], total: int
) -> set[tuple[int, int]]:
    cap = max(1, int(total * MAX_SHARE))
    floor = max(1, math.ceil(total * MIN_SHARE))
    ai: list[tuple[float, int, int]] = []
    fill: list[tuple[float, int, int]] = []
    prev_end_of_sentence = True
    for ci, chunk in enumerate(chunks):
        flags = preferred[ci] if ci < len(preferred) else []
        scored = []
        for wi, word in enumerate(chunk):
            scored.append((_score(word, sentence_start=prev_end_of_sentence), wi))
            prev_end_of_sentence = word.rstrip().endswith((".", "!", "?"))
        mine = [sw for sw in scored if _flagged(flags, sw[1])]
        if mine:
            top = max(mine)
            ai.append((top[0], ci, top[1]))
        else:
            top = max(scored)
            if top[0] > 0:
                fill.append((top[0], ci, top[1]))
    ai.sort(reverse=True)
    fill.sort(reverse=True)
    chosen = ai[:cap]
    for b in fill:
        if len(chosen) >= floor:
            break
        chosen.append(b)
    return {(ci, wi) for _, ci, wi in chosen}


def _flagged(flags: list[bool], wi: int) -> bool:
    return wi < len(flags) and bool(flags[wi])
