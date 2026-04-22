from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.music import build_music_prep_command, select_track
from reelforge_core.errors import MusicNotFoundError
from reelforge_core.models import (
    ComposeConfig,
    MusicTrack,
    RankedReel,
    ReelScores,
)


def _reel(mood: str, cid: str = "abc") -> RankedReel:
    return RankedReel(
        candidate_id=cid,
        scene_indices=[0],
        start_sec=0.0,
        end_sec=30.0,
        duration_sec=30.0,
        title="T",
        hook="H",
        justification="J",
        scores=ReelScores(
            narrative_coherence=70,
            hook_strength=70,
            emotional_payoff=70,
            standalone_clarity=70,
        ),
        overall=70.0,
        rank=1,
        suggested_mood=mood,  # type: ignore[arg-type]
    )


def _track(id_: str, mood: str) -> MusicTrack:
    return MusicTrack(
        id=id_,
        path=f"/app/assets/music/{id_}.mp3",
        source="bundled",
        bpm=100,
        mood=mood,  # type: ignore[arg-type]
        duration_sec=90.0,
        license="CC0",
        attribution=None,
    )


def test_select_track_matches_mood() -> None:
    library = [_track("joyful-01", "joyful"), _track("calm-01", "calm")]
    pick = select_track(library, ComposeConfig(), _reel("joyful"))
    assert pick is not None and pick.mood == "joyful"


def test_select_track_falls_back_to_neutral() -> None:
    library = [_track("neutral-01", "neutral")]
    pick = select_track(library, ComposeConfig(), _reel("triumphant"))
    assert pick is not None and pick.id == "neutral-01"


def test_select_track_returns_none_when_nothing_matches() -> None:
    library = [_track("calm-01", "calm")]
    pick = select_track(library, ComposeConfig(), _reel("tense"))
    assert pick is None


def test_select_track_respects_no_music() -> None:
    library = [_track("joyful-01", "joyful")]
    pick = select_track(library, ComposeConfig(no_music=True), _reel("joyful"))
    assert pick is None


def test_select_track_specific_id_missing_raises() -> None:
    library = [_track("joyful-01", "joyful")]
    with pytest.raises(MusicNotFoundError):
        select_track(library, ComposeConfig(music_track_id="nonexistent"), _reel("joyful"))


def test_select_track_deterministic_across_multiple_matches() -> None:
    library = [
        _track("joyful-01", "joyful"),
        _track("joyful-02", "joyful"),
        _track("joyful-03", "joyful"),
    ]
    cfg = ComposeConfig(seed=42)
    reel = _reel("joyful", cid="fixed-candidate")
    first = select_track(library, cfg, reel)
    second = select_track(library, cfg, reel)
    assert first is not None and second is not None
    assert first.id == second.id


def test_music_prep_command_has_loop_and_trim() -> None:
    track = _track("joyful-01", "joyful")
    cmd = build_music_prep_command(
        track=track,
        out_path=Path("/tmp/music.wav"),
        target_duration_sec=45.0,
        config=ComposeConfig(),
    )
    assert "-stream_loop" in cmd and "-1" in cmd
    assert "-t" in cmd and "45.000" in cmd
    af = cmd[cmd.index("-af") + 1]
    assert "afade=t=in:st=0:d=0.5" in af
    assert "afade=t=out:st=44.000:d=1.0" in af
    assert "volume=" in af
