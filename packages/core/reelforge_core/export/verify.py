"""Post-transcode output verification. FFmpeg exits 0 on plenty of broken files;
the real correctness gate is ffprobe.

Do NOT delete a failed output — keep it for forensics.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reelforge_core.errors import OutputVerificationError
from reelforge_core.export.presets import expected_fourcc
from reelforge_core.models import PresetSpec

log = logging.getLogger(__name__)

MIN_SIZE_BYTES = 64 * 1024  # 64 KB: catches truly empty outputs. Real 30s+ reels far exceed this.


@dataclass(frozen=True)
class VerifiedOutput:
    duration_sec: float
    width: int
    height: int
    fps: float
    file_size_bytes: int
    video_codec: str
    audio_codec: str
    pixel_format: str
    video_codec_tag: str


def _ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OutputVerificationError(
            f"ffprobe failed on {path}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OutputVerificationError(f"ffprobe returned invalid JSON: {exc}") from exc


def _parse_fps(rate: str | None) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    return float(rate)


def _codec_map(preset_codec: str) -> str:
    """Map our PresetSpec codec name to ffprobe's `codec_name`."""
    return {
        "libx264": "h264",
        "libx265": "hevc",
        "prores_ks": "prores",
        "aac": "aac",
        "pcm_s16le": "pcm_s16le",
    }[preset_codec]


def verify_export(
    output_path: Path,
    preset: PresetSpec,
    *,
    mezzanine_duration_sec: float | None = None,
) -> VerifiedOutput:
    if not output_path.exists():
        raise OutputVerificationError(f"output missing: {output_path}")
    size = output_path.stat().st_size
    if size < MIN_SIZE_BYTES:
        raise OutputVerificationError(
            f"output size {size} bytes is below the {MIN_SIZE_BYTES}-byte floor"
        )

    data = _ffprobe(output_path)
    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise OutputVerificationError(
            f"expected 1 video stream, got {len(video_streams)}"
        )
    if len(audio_streams) != 1:
        raise OutputVerificationError(
            f"expected 1 audio stream, got {len(audio_streams)}"
        )

    v = video_streams[0]
    a = audio_streams[0]

    # Codec
    expected_v = _codec_map(preset.video_codec)
    if v.get("codec_name") != expected_v:
        raise OutputVerificationError(
            f"video codec mismatch: got {v.get('codec_name')!r}, expected {expected_v!r}"
        )
    expected_a = _codec_map(preset.audio_codec)
    if a.get("codec_name") != expected_a:
        raise OutputVerificationError(
            f"audio codec mismatch: got {a.get('codec_name')!r}, expected {expected_a!r}"
        )

    # Pixel format
    if v.get("pix_fmt") != preset.video_pixel_format:
        raise OutputVerificationError(
            f"pixel format mismatch: got {v.get('pix_fmt')!r}, "
            f"expected {preset.video_pixel_format!r}"
        )

    # Codec tag (hvc1 for h265, apcn/apch for ProRes, any for h264)
    expected_tag = expected_fourcc(preset)
    actual_tag = v.get("codec_tag_string", "")
    if expected_tag is not None and actual_tag != expected_tag:
        raise OutputVerificationError(
            f"codec_tag mismatch: got {actual_tag!r}, expected {expected_tag!r}"
        )

    # Audio presence — duration must be > 0 and nb_samples (if reported) must be > 0
    a_duration_raw = a.get("duration") or data.get("format", {}).get("duration") or "0"
    a_duration = float(a_duration_raw)
    if a_duration <= 0:
        raise OutputVerificationError("audio stream has zero duration")
    if "nb_samples" in a and int(a["nb_samples"]) == 0:
        raise OutputVerificationError("audio stream reports 0 samples")

    # Duration check against mezzanine (if caller passed one).
    container_duration = float(data.get("format", {}).get("duration", 0))
    if mezzanine_duration_sec is not None and container_duration > 0:
        if abs(container_duration - mezzanine_duration_sec) > 0.2:
            raise OutputVerificationError(
                f"duration drift {container_duration:.3f}s vs mezzanine "
                f"{mezzanine_duration_sec:.3f}s exceeds 0.2s tolerance"
            )

    return VerifiedOutput(
        duration_sec=container_duration,
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        fps=_parse_fps(v.get("avg_frame_rate") or v.get("r_frame_rate")),
        file_size_bytes=size,
        video_codec=v.get("codec_name", "unknown"),
        audio_codec=a.get("codec_name", "unknown"),
        pixel_format=v.get("pix_fmt", "unknown"),
        video_codec_tag=actual_tag,
    )


def sanity_check_size(
    output_size_bytes: int,
    mezzanine_size_bytes: int,
    preset: PresetSpec,
) -> None:
    """Warn (don't fail) when output size is > 3× off from the expected ratio."""
    if mezzanine_size_bytes <= 0:
        return
    expected = mezzanine_size_bytes * preset.typical_size_ratio_vs_mezzanine
    if expected <= 0:
        return
    ratio = output_size_bytes / expected
    if ratio > 3.0 or ratio < 1 / 3.0:
        log.warning(
            "export size %d bytes is %.2fx the expected ~%d (preset %s)",
            output_size_bytes,
            ratio,
            int(expected),
            preset.id,
        )
