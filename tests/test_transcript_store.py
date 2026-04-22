from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core import transcript_store
from reelforge_core.models import Transcript, TranscriptSegment, TranscriptWord


@pytest.fixture
def ts_env(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    if hasattr(transcript_store._LOCAL, "conn"):
        try:
            transcript_store._LOCAL.conn.close()
        except Exception:
            pass
        del transcript_store._LOCAL.conn
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(isolated_data_dir))
    return isolated_data_dir


def _make_t() -> Transcript:
    return Transcript(
        language="en",
        language_probability=0.99,
        duration=3.0,
        segments=[
            TranscriptSegment(
                start=0.0,
                end=1.0,
                text=" hello",
                words=[TranscriptWord(start=0.1, end=0.3, word=" hello", probability=0.9)],
            ),
            TranscriptSegment(
                start=1.0,
                end=2.0,
                text=" world",
                words=[TranscriptWord(start=1.1, end=1.5, word=" world", probability=0.9)],
            ),
        ],
    )


def test_validate_accepts_good_transcript() -> None:
    transcript_store.validate_transcript(_make_t())


def test_validate_rejects_non_monotonic_segments() -> None:
    t = _make_t()
    t.segments[1] = t.segments[1].model_copy(update={"start": 0.4, "end": 0.6})
    with pytest.raises(ValueError):
        transcript_store.validate_transcript(t)


def test_validate_rejects_word_end_before_start() -> None:
    t = _make_t()
    t.segments[0].words[0] = t.segments[0].words[0].model_copy(
        update={"start": 0.5, "end": 0.3}
    )
    with pytest.raises(ValueError):
        transcript_store.validate_transcript(t)


def test_validate_rejects_negative_start() -> None:
    t = _make_t()
    t.segments[0].words[0] = t.segments[0].words[0].model_copy(update={"start": -0.1})
    with pytest.raises(ValueError):
        transcript_store.validate_transcript(t)


async def test_save_and_load_roundtrip(ts_env: Path) -> None:
    orig = _make_t()
    await transcript_store.save_override("asset-A", orig)
    back = await transcript_store.load_override("asset-A")
    assert back is not None
    assert back.language == "en"
    assert len(back.segments) == 2


async def test_load_missing_returns_none(ts_env: Path) -> None:
    assert await transcript_store.load_override("nonexistent") is None


async def test_delete_override(ts_env: Path) -> None:
    await transcript_store.save_override("asset-B", _make_t())
    assert await transcript_store.load_override("asset-B") is not None
    n = await transcript_store.delete_override("asset-B")
    assert n == 1
    assert await transcript_store.load_override("asset-B") is None
    # Re-delete is a no-op
    assert await transcript_store.delete_override("asset-B") == 0


def test_load_override_sync_parallels_async(ts_env: Path) -> None:
    # The compose pipeline uses the sync loader; make sure it returns the same
    # thing as the async loader for the same stored data.
    transcript_store._save_sync("sync-1", _make_t())
    got = transcript_store.load_override_sync("sync-1")
    assert got is not None
    assert got.duration == 3.0
