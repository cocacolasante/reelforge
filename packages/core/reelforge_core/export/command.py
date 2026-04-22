"""Pure FFmpeg command construction for export presets. No filters, ever."""

from __future__ import annotations

from pathlib import Path

from reelforge_core.models import PresetSpec


def build_export_command(
    mezzanine_path: Path,
    output_path: Path,
    preset: PresetSpec,
) -> list[str]:
    """Return a ready-to-run `ffmpeg` argv list for the given preset.

    Pure function. No I/O. Caller is responsible for the skip-if-exists logic.
    """
    args: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(mezzanine_path),
        # Explicit stream map. The mezzanine always has exactly one V + one A.
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        # Video
        "-c:v",
        preset.video_codec,
        "-pix_fmt",
        preset.video_pixel_format,
    ]
    for key, val in preset.video_params.items():
        args += [f"-{key}", str(val)]

    # Audio
    args += ["-c:a", preset.audio_codec]
    if preset.audio_bitrate_kbps is not None:
        args += ["-b:a", f"{preset.audio_bitrate_kbps}k"]

    # Container flags (movflags, tag:v, etc.)
    for key, val in preset.container_flags.items():
        args += [f"-{key}", str(val)]

    # Deterministic metadata — no wall clock.
    args += [
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-metadata",
        "encoder=reelforge",
    ]

    args.append(str(output_path))
    return args
