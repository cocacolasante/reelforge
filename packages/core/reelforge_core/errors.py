"""Analysis error hierarchy. The pipeline raises one of these per stage; the worker
catches `AnalysisError` and records stage context on the job."""

from __future__ import annotations


class AnalysisError(Exception):
    """Base class for analysis-pipeline failures."""


class ProbeError(AnalysisError):
    """ffprobe failed or the source has no usable video stream."""


class SceneDetectionError(AnalysisError):
    """PySceneDetect failed or thumbnail extraction failed."""


class TranscriptionError(AnalysisError):
    """Audio extraction failed or Whisper raised."""


class LoudnessError(AnalysisError):
    """ebur128 pass failed or emitted no parseable lines."""


class SemanticsError(AnalysisError):
    """Anthropic calls failed after all retries, or a tool-use response was malformed."""


# ---------------------------------------------------------------------------
# Phase 2: reel selection
# ---------------------------------------------------------------------------


class SelectionError(Exception):
    """Base class for reel-selection failures."""


class RankingError(SelectionError):
    """LLM ranking call failed after retries, or the response was malformed."""


class NoCandidatesError(SelectionError):
    """No 30-60s spans available in the source. Retained for API semantic clarity;
    the pipeline currently handles this case by writing an empty ReelSelection."""


# ---------------------------------------------------------------------------
# Phase 3: composition
# ---------------------------------------------------------------------------


class ComposeError(Exception):
    """Base class for composition failures."""


class FFmpegError(ComposeError):
    """ffmpeg exited non-zero. Carries stderr tail and the command line."""

    def __init__(self, message: str, *, stderr: str = "", cmdline: str = ""):
        super().__init__(message)
        self.stderr = stderr
        self.cmdline = cmdline

    def __str__(self) -> str:
        base = super().__str__()
        if self.stderr:
            base += f"\n--- stderr tail ---\n{self.stderr}"
        if self.cmdline:
            base += f"\n--- cmdline ---\n{self.cmdline}"
        return base


class MusicNotFoundError(ComposeError):
    """User asked for a specific music_track_id that isn't in the library."""


class GraphError(ComposeError):
    """FilterGraph construction error (e.g. duplicate output labels)."""


# ---------------------------------------------------------------------------
# Phase 4: export
# ---------------------------------------------------------------------------


class ExportError(Exception):
    """Base class for export failures."""


class PresetNotFoundError(ExportError):
    """Unknown preset id."""


class MezzanineNotFoundError(ExportError):
    """The referenced reel's mezzanine.mp4 doesn't exist yet."""


class OutputVerificationError(ExportError):
    """The transcode completed but produced a file that fails shape checks."""
