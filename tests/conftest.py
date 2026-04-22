"""Shared fixtures: synthesize small MP4s via ffmpeg and patch Anthropic.

All fixtures use generated clips rather than shipped binaries so the repo stays small.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="session")
def tiny_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("media") / "tiny.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:d=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip(f"ffmpeg could not synthesize test clip: {result.stderr}")
    return out


def _run_ffmpeg(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.skip(f"ffmpeg command failed: {' '.join(cmd)}\n{r.stderr}")


@pytest.fixture(scope="session")
def multiscene_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 6s clip with 3 hard color cuts + a sine tone. Wide enough to detect scenes."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    tmp = tmp_path_factory.mktemp("media")
    # Three 2s solid colors concatenated. Use -filter_complex with xfade=duration=0
    # (i.e. hard cut) — easiest is to generate three files then concat.
    parts = []
    colors = ["red", "green", "blue"]
    for i, c in enumerate(colors):
        p = tmp / f"part{i}.mp4"
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={c}:s=320x240:d=2:r=25",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(p),
            ]
        )
        parts.append(p)
    # Concat.
    concat_list = tmp / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p}'" for p in parts))
    out = tmp / "multiscene.mp4"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def silent_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path_factory.mktemp("media") / "silent.mp4"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=3:r=25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(out),
        ]
    )
    return out


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect REELFORGE_DATA_DIR for a single test so the real DB isn't touched."""
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(tmp_path))
    # Clear the sqlite thread-local so a fresh connection opens inside tmp_path.
    from reelforge_core import db as _db

    if hasattr(_db._LOCAL, "conn"):
        try:
            _db._LOCAL.conn.close()
        except Exception:
            pass
        del _db._LOCAL.conn
    # Also reset cached DB_PATH module attribute.
    _db.DB_PATH = Path(tmp_path) / "reelforge.db"
    return tmp_path


# ---------------------------------------------------------------------------
# Fake Anthropic client for semantics tests
# ---------------------------------------------------------------------------


@dataclass
class _ToolUseBlock:
    type: str
    input: dict


@dataclass
class _Usage:
    input_tokens: int = 42
    output_tokens: int = 21


class FakeAnthropicClient:
    """Stand-in for anthropic.AsyncAnthropic used in semantics tests."""

    def __init__(self, responder=None):
        self.calls: list[dict] = []
        self.messages = self  # so `.messages.create(...)` works
        self._responder = responder or self._default_responder

    @staticmethod
    def _default_responder(scene_index: int) -> dict:
        moods = [
            "calm",
            "tense",
            "joyful",
            "somber",
            "energetic",
            "mysterious",
            "romantic",
            "triumphant",
            "melancholic",
            "neutral",
        ]
        energies = ["low", "medium", "high"]
        return {
            "summary": f"Scene {scene_index} summary of the moment.",
            "tags": [f"tag{scene_index}a", f"tag{scene_index}b", "generic"],
            "mood": moods[scene_index % len(moods)],
            "has_speech": scene_index % 2 == 0,
            "visual_energy": energies[scene_index % len(energies)],
        }

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        # Derive scene index from user text for variety across scenes.
        idx = 0
        try:
            text_block = kwargs["messages"][0]["content"][1]["text"]
            idx = int(text_block.split("Scene ")[1].split(" of ")[0])
        except Exception:
            pass
        data = self._responder(idx)
        return SimpleNamespace(
            content=[_ToolUseBlock(type="tool_use", input=data)],
            stop_reason="tool_use",
            usage=_Usage(),
        )


@pytest.fixture
def fake_anthropic() -> FakeAnthropicClient:
    return FakeAnthropicClient()
