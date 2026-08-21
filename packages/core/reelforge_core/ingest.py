"""Ingestion: probe media with ffprobe and assign content-addressed asset IDs."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HASH_WINDOW_BYTES = 1 * 1024 * 1024  # 1 MiB head + 1 MiB tail

# ffprobe reports a still image as a one-frame video stream in an image
# container, so these identify "this asset is a photo, not footage".
PHOTO_CODECS = {
    "mjpeg", "png", "webp", "bmp", "tiff", "gif", "jpeg2000", "ppm", "pgm",
}
PHOTO_CONTAINERS = {
    "image2", "png_pipe", "webp_pipe", "jpeg_pipe", "mjpeg", "gif",
    "bmp_pipe", "tiff_pipe", "image2pipe",
}


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None
    bit_rate: int | None
    container: str
    color_transfer: str | None = None


@dataclass(frozen=True)
class MediaAsset:
    id: str  # sha256 of first + last 1 MiB + byte length
    path: Path
    size_bytes: int
    probe: ProbeResult

    @property
    def has_audio(self) -> bool:
        return self.probe.audio_codec is not None

    @property
    def fps(self) -> float:
        return self.probe.fps

    @property
    def is_photo(self) -> bool:
        return is_photo_probe(self.probe)

    @property
    def is_audio(self) -> bool:
        """Audio-only media (voiceover takes, music): no picture at all."""
        return self.probe.video_codec == "none" and self.probe.audio_codec is not None

    @classmethod
    def from_path(cls, path: str | Path) -> "MediaAsset":
        """Probe and return a MediaAsset. Alias for `probe()` with a class-method face."""
        return probe(path)


class ProbeError(RuntimeError):
    """Raised when ffprobe fails or returns unparseable output."""


def is_photo_probe(pr: "ProbeResult") -> bool:
    """True when the probed file is a still image rather than footage.

    Checks the container first (`image2` and friends) because a single-frame
    MJPEG *video* is conceivable; a photo is always in an image container.
    """
    containers = {c.strip() for c in (pr.container or "").split(",")}
    if containers & PHOTO_CONTAINERS:
        return True
    return pr.video_codec in PHOTO_CODECS and pr.audio_codec is None


def _content_id(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as f:
        head = f.read(min(HASH_WINDOW_BYTES, size))
        h.update(head)
        if size > HASH_WINDOW_BYTES:
            f.seek(max(0, size - HASH_WINDOW_BYTES))
            tail = f.read(HASH_WINDOW_BYTES)
            h.update(tail)
    h.update(size.to_bytes(8, "big"))
    return h.hexdigest()


def _run_ffprobe(path: Path) -> dict:
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
    log.info("ffprobe %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed ({result.returncode}): {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {exc}") from exc


def _parse_fps(rate: str | None) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    return float(rate)


def probe(path: str | Path) -> MediaAsset:
    """Probe `path` and return a MediaAsset with duration, resolution, fps, and content id."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if not p.is_file():
        raise ProbeError(f"not a file: {p}")

    raw = _run_ffprobe(p)
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise ProbeError(f"no video or audio stream in {p}")

    bit_rate = int(fmt["bit_rate"]) if fmt.get("bit_rate") else None
    if video is None:
        # Audio-only (voiceover take, music). Zero picture geometry; the
        # rest of the pipeline keys off `video_codec == "none"`.
        duration = float(fmt.get("duration") or (audio or {}).get("duration") or 0.0)
        pr = ProbeResult(
            duration_s=duration,
            width=0,
            height=0,
            fps=0.0,
            video_codec="none",
            audio_codec=audio.get("codec_name") if audio else None,
            bit_rate=bit_rate,
            container=fmt.get("format_name", "unknown"),
            color_transfer=None,
        )
        return MediaAsset(
            id=_content_id(p), path=p.resolve(), size_bytes=p.stat().st_size, probe=pr
        )

    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))

    pr = ProbeResult(
        duration_s=duration,
        width=width,
        height=height,
        fps=fps,
        video_codec=video.get("codec_name", "unknown"),
        audio_codec=audio.get("codec_name") if audio else None,
        bit_rate=bit_rate,
        container=fmt.get("format_name", "unknown"),
        color_transfer=video.get("color_transfer"),
    )

    return MediaAsset(
        id=_content_id(p),
        path=p.resolve(),
        size_bytes=p.stat().st_size,
        probe=pr,
    )


def asset_to_dict(asset: MediaAsset) -> dict:
    """Serializable representation of a MediaAsset — what gets written to probe.json."""
    return {
        "id": asset.id,
        "path": str(asset.path),
        "size_bytes": asset.size_bytes,
        "has_audio": asset.has_audio,
        "probe": asdict(asset.probe),
    }
