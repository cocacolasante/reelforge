"""Music library loader, deterministic mood-based selection, trim+loop+fade prep."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from reelforge_core.compose.graph import run_ffmpeg
from reelforge_core.errors import MusicNotFoundError
from reelforge_core.models import ComposeConfig, MusicTrack, RankedReel

log = logging.getLogger(__name__)

BUNDLED_MANIFEST = Path("/app/assets/music/manifest.json")
USER_MANIFEST_DEFAULT = Path("/data/music/manifest.json")


def load_music_library(
    bundled_path: Path | None = None,
    user_path: Path | None = None,
) -> list[MusicTrack]:
    """Merge bundled + user tracks. On id collision, user takes precedence."""
    bundled_path = bundled_path or BUNDLED_MANIFEST
    user_path = user_path or Path(
        os.environ.get("REELFORGE_MUSIC_DIR", "/data/music")
    ) / "manifest.json"

    library: dict[str, MusicTrack] = {}

    if bundled_path.exists():
        library.update(_load_manifest(bundled_path, "bundled"))
    if user_path.exists():
        for k, v in _load_manifest(user_path, "user").items():
            library[k] = v  # user wins on conflict

    return list(library.values())


def _load_manifest(path: Path, source: str) -> dict[str, MusicTrack]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("failed to parse music manifest %s: %s", path, exc)
        return {}
    out: dict[str, MusicTrack] = {}
    for entry in raw.get("tracks", []):
        try:
            track = MusicTrack(source=source, **entry)
        except Exception as exc:
            log.warning("invalid track in %s: %s", path, exc)
            continue
        out[track.id] = track
    return out


def select_track(
    library: list[MusicTrack],
    config: ComposeConfig,
    reel: RankedReel,
) -> MusicTrack | None:
    """Deterministic mood-matched selection. Returns None if nothing matches
    (not an error; the pipeline just renders without music)."""
    if config.no_music:
        return None
    if config.music_track_id is not None:
        match = next((t for t in library if t.id == config.music_track_id), None)
        if match is None:
            raise MusicNotFoundError(
                f"music_track_id={config.music_track_id!r} not found in library"
            )
        return match

    matches = [t for t in library if t.mood == reel.suggested_mood]
    if not matches:
        matches = [t for t in library if t.mood == "neutral"]
    if not matches:
        return None
    # Real tracks beat bundled placeholders: the bundled set is synthesized
    # waveform filler, so whenever the user library covers this mood, pick
    # only from it.
    user_matches = [t for t in matches if t.source == "user"]
    if user_matches:
        matches = user_matches
    # Stable pick by reel.candidate_id + config.seed
    key = f"{reel.candidate_id}|{config.seed}".encode("utf-8")
    idx = int(hashlib.sha256(key).hexdigest(), 16) % len(matches)
    return sorted(matches, key=lambda t: t.id)[idx]


def _linear_gain(db: float) -> float:
    return 10 ** (db / 20.0)


def build_music_prep_command(
    *,
    track: MusicTrack,
    out_path: Path,
    target_duration_sec: float,
    config: ComposeConfig,
) -> list[str]:
    gain = _linear_gain(config.music_volume_db)
    fade_out_start = max(0.0, target_duration_sec - 1.0)
    af = (
        "aresample=48000,"
        "aformat=channel_layouts=stereo,"
        "afade=t=in:st=0:d=0.5,"
        f"afade=t=out:st={fade_out_start:.3f}:d=1.0,"
        f"volume={gain:.6f}"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        track.path,
        "-t",
        f"{target_duration_sec:.3f}",
        "-af",
        af,
        "-c:a",
        "pcm_s16le",
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(out_path),
    ]


def prepare_music(
    track: MusicTrack,
    target_duration_sec: float,
    config: ComposeConfig,
    reel_dir: Path,
    log_file: Path,
) -> Path:
    """Produce music.wav sized to `target_duration_sec` with 0.5s in / 1.0s out fades.

    Deterministic for a given `(track, duration, music_volume_db)`, so we cache
    the prepped WAV and hard-link from the reel dir on subsequent runs.
    """
    import shutil as _shutil

    from reelforge_core import cache as file_cache

    out_path = reel_dir / "music.wav"
    cache_key = file_cache.compute_key(
        "music",
        {
            "track_id": track.id,
            "dur": f"{target_duration_sec:.3f}",
            "vol_db": f"{config.music_volume_db:.1f}",
        },
    )
    cached = file_cache.lookup(cache_key)
    if cached is not None:
        try:
            if out_path.exists():
                out_path.unlink()
            import os as _os

            _os.link(cached, out_path)
        except OSError:
            _shutil.copy2(cached, out_path)
        return out_path

    cmd = build_music_prep_command(
        track=track,
        out_path=out_path,
        target_duration_sec=target_duration_sec,
        config=config,
    )
    run_ffmpeg(cmd, log_file=log_file)
    try:
        cache_target = file_cache.path_for("music", cache_key, "wav")
        _shutil.copy2(out_path, cache_target)
        file_cache.register(cache_key, "music", cache_target)
        file_cache.evict_if_over_cap(
            "music", file_cache.cap_from_env("music", 2.0)
        )
    except Exception:  # pragma: no cover
        log.exception("music cache write failed")
    return out_path
