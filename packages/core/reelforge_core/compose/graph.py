"""FFmpeg filter-graph DSL + subprocess runner.

Building `-filter_complex` by string concatenation is how you end up with
`Cannot find a matching stream for unlabeled input pad 0 on filter`. This tiny
typed DSL keeps labels unique, escapes argument values, and serialises a full
graph to a single semicolon-separated string that FFmpeg consumes.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from reelforge_core.errors import FFmpegError, GraphError

log = logging.getLogger(__name__)


def _ffescape(value: object) -> str:
    """Escape a single filter-argument value.

    Backslashes must go first so subsequent replacements don't double-escape.
    """
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", r"\:")
    s = s.replace(",", r"\,")
    s = s.replace("'", r"\'")
    s = s.replace("[", r"\[")
    s = s.replace("]", r"\]")
    return s


@dataclass
class FilterNode:
    filter_name: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    args: dict[str, str | int | float] = field(default_factory=dict)

    def serialize(self) -> str:
        ins = "".join(self.inputs)
        outs = "".join(self.outputs)
        if self.args:
            arg_str = ":".join(f"{k}={_ffescape(v)}" for k, v in self.args.items())
            return f"{ins}{self.filter_name}={arg_str}{outs}"
        return f"{ins}{self.filter_name}{outs}"


@dataclass
class FilterGraph:
    nodes: list[FilterNode] = field(default_factory=list)
    _label_counter: int = 0
    _declared_outputs: set[str] = field(default_factory=set)

    def new_label(self, prefix: str = "v") -> str:
        self._label_counter += 1
        return f"[{prefix}{self._label_counter}]"

    def add(self, node: FilterNode) -> None:
        for out in node.outputs:
            if out in self._declared_outputs:
                raise GraphError(f"duplicate output label {out}")
            self._declared_outputs.add(out)
        self.nodes.append(node)

    def extend(self, nodes: Iterable[FilterNode]) -> None:
        for n in nodes:
            self.add(n)

    def serialize(self) -> str:
        return ";".join(n.serialize() for n in self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)


def run_ffmpeg(
    args: list[str],
    *,
    cwd: Path | None = None,
    log_file: Path | None = None,
    timeout_sec: float | None = None,
) -> subprocess.CompletedProcess:
    """Invoke ffmpeg and raise FFmpegError with stderr tail on non-zero exit."""
    cmdline = " ".join(shlex.quote(a) for a in args)
    if log_file is not None:
        _append_log(log_file, cmdline)
    log.debug("ffmpeg: %s", cmdline)
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            f"ffmpeg timed out after {timeout_sec}s",
            stderr=(exc.stderr or "").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            cmdline=cmdline,
        ) from exc
    if result.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited {result.returncode}",
            stderr=result.stderr[-4000:],
            cmdline=cmdline,
        )
    return result


def _append_log(log_file: Path, cmdline: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # Cap at 10 MB; rotate to .1.
    if log_file.exists() and log_file.stat().st_size > 10 * 1024 * 1024:
        rotated = log_file.with_name(log_file.name + ".1")
        log_file.replace(rotated)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(cmdline + "\n\n")


def ffmpeg_version() -> str:
    """Return the ffmpeg major.minor version string, or 'unknown' if not resolvable."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    import re

    m = re.search(r"ffmpeg version (\S+)", result.stdout)
    return m.group(1) if m else "unknown"
