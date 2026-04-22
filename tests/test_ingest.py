from __future__ import annotations

from pathlib import Path

from reelforge_core import probe


def test_probe_reads_duration_resolution_fps(tiny_mp4: Path) -> None:
    asset = probe(tiny_mp4)
    assert asset.id and len(asset.id) == 64  # sha256 hex
    assert asset.size_bytes > 0
    assert asset.probe.width == 320
    assert asset.probe.height == 240
    assert asset.probe.duration_s > 0
    assert asset.probe.fps > 0
    assert asset.probe.video_codec == "h264"
    assert asset.probe.audio_codec == "aac"


def test_probe_id_is_stable(tiny_mp4: Path) -> None:
    a = probe(tiny_mp4)
    b = probe(tiny_mp4)
    assert a.id == b.id


def test_probe_missing_file() -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        probe(Path("/tmp/definitely-not-here-reelforge.mp4"))
