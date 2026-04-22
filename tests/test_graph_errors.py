"""compose/graph.py: FFmpegError construction + GraphError semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core.compose.graph import (
    FilterGraph,
    FilterNode,
    ffmpeg_version,
    run_ffmpeg,
)
from reelforge_core.errors import FFmpegError, GraphError


def test_filtergraph_new_label_increments() -> None:
    g = FilterGraph()
    a = g.new_label()
    b = g.new_label()
    c = g.new_label("audio")
    assert a != b
    assert a.startswith("[v") and b.startswith("[v")
    assert c.startswith("[audio")


def test_filtergraph_rejects_duplicate_label_across_nodes() -> None:
    g = FilterGraph()
    g.add(FilterNode("null", inputs=["[0:v]"], outputs=["[x]"]))
    g.add(FilterNode("null", inputs=["[x]"], outputs=["[y]"]))
    with pytest.raises(GraphError):
        g.add(FilterNode("null", inputs=["[y]"], outputs=["[x]"]))


def test_filtergraph_extend() -> None:
    g = FilterGraph()
    nodes = [
        FilterNode("null", inputs=["[0:v]"], outputs=["[a]"]),
        FilterNode("null", inputs=["[a]"], outputs=["[b]"]),
    ]
    g.extend(nodes)
    assert len(g) == 2


def test_filtergraph_rejects_dup_via_extend() -> None:
    g = FilterGraph()
    g.add(FilterNode("null", inputs=["[0:v]"], outputs=["[x]"]))
    with pytest.raises(GraphError):
        g.extend(
            [
                FilterNode("null", inputs=["[x]"], outputs=["[x]"]),
            ]
        )


def test_ffmpeg_error_string_includes_stderr_and_cmdline() -> None:
    e = FFmpegError("boom", stderr="the error tail", cmdline="ffmpeg -i foo bar")
    s = str(e)
    assert "boom" in s
    assert "the error tail" in s
    assert "ffmpeg -i foo bar" in s


def test_run_ffmpeg_raises_on_nonzero(tmp_path: Path) -> None:
    # Deliberately invalid args → ffmpeg exits non-zero.
    with pytest.raises(FFmpegError):
        run_ffmpeg(["ffmpeg", "-hide_banner", "-i", "/nonexistent-path.mp4", str(tmp_path / "out.mp4")])


def test_run_ffmpeg_logs_cmdline_to_file(tmp_path: Path) -> None:
    log = tmp_path / "commands.log"
    try:
        run_ffmpeg(
            ["ffmpeg", "-hide_banner", "-i", "/nonexistent.mp4", str(tmp_path / "out.mp4")],
            log_file=log,
        )
    except FFmpegError:
        pass
    assert log.exists()
    assert "ffmpeg" in log.read_text()


def test_ffmpeg_version_returns_string() -> None:
    v = ffmpeg_version()
    # Inside the test container ffmpeg is present and returns a version like "5.1.x".
    assert isinstance(v, str)
    assert v != "" and v != "unknown"
