"""Four opinionated delivery presets. Defined in code — not user-editable.

Bump PRESET_SPEC_VERSION when a preset's parameters change so existing exports
are treated as stale by the skip-if-exists check.

ProRes profile table (prores_ks `-profile:v N`):
    0 = Proxy        (fourcc apco)
    1 = LT           (fourcc apcs)
    2 = 422 Standard (fourcc apcn)
    3 = 422 HQ       (fourcc apch)
    4 = 4444         (fourcc ap4h, needs yuva444p10le)
    5 = 4444 XQ
"""

from __future__ import annotations

from reelforge_core.errors import PresetNotFoundError
from reelforge_core.models import PresetSpec

PRESET_SPEC_VERSION = "1"


PRESETS: dict[str, PresetSpec] = {
    "mp4_h264_social": PresetSpec(
        id="mp4_h264_social",
        container="mp4",
        video_codec="libx264",
        video_pixel_format="yuv420p",
        video_params={
            "crf": 20,
            "preset": "medium",
            "profile:v": "high",
            "level": "4.1",
        },
        audio_codec="aac",
        audio_bitrate_kbps=192,
        container_flags={"movflags": "+faststart"},
        target_use="Instagram/TikTok/YouTube Shorts delivery",
        typical_size_ratio_vs_mezzanine=0.6,
    ),
    "mp4_h265_hq": PresetSpec(
        id="mp4_h265_hq",
        container="mp4",
        video_codec="libx265",
        video_pixel_format="yuv420p",
        video_params={
            "crf": 22,
            "preset": "medium",
            "x265-params": "log-level=error",
        },
        audio_codec="aac",
        audio_bitrate_kbps=256,
        # hvc1 tag so Apple players (Safari, QuickTime, iOS) recognize the file.
        # Without it the video plays in VLC/Chrome and silently fails in Safari.
        container_flags={"movflags": "+faststart", "tag:v": "hvc1"},
        target_use="High-efficiency modern distribution",
        typical_size_ratio_vs_mezzanine=0.35,
    ),
    "mov_prores_422": PresetSpec(
        id="mov_prores_422",
        container="mov",
        video_codec="prores_ks",
        video_pixel_format="yuv422p10le",  # mandatory; yuv420p produces broken ProRes
        video_params={
            "profile:v": "2",  # ProRes 422 Standard → fourcc apcn
            "vendor": "apl0",  # Apple-compatible vendor tag; FCP requires it
        },
        audio_codec="pcm_s16le",
        audio_bitrate_kbps=None,
        container_flags={},
        target_use="Editorial handoff (ProRes 422 Standard)",
        typical_size_ratio_vs_mezzanine=8.0,
    ),
    "mov_prores_hq": PresetSpec(
        id="mov_prores_hq",
        container="mov",
        video_codec="prores_ks",
        video_pixel_format="yuv422p10le",
        video_params={
            "profile:v": "3",  # ProRes 422 HQ → fourcc apch
            "vendor": "apl0",
        },
        audio_codec="pcm_s16le",
        audio_bitrate_kbps=None,
        container_flags={},
        target_use="Editorial handoff (ProRes 422 HQ)",
        typical_size_ratio_vs_mezzanine=12.0,
    ),
}


# FourCC tag each ProRes profile produces — used by verify.py and assertable
# via `ffprobe -show_streams | codec_tag_string`.
PRORES_FOURCC: dict[str, str] = {
    "0": "apco",  # Proxy
    "1": "apcs",  # LT
    "2": "apcn",  # 422 Standard
    "3": "apch",  # 422 HQ
    "4": "ap4h",  # 4444
    "5": "ap4x",  # 4444 XQ
}


def get_preset(preset_id: str) -> PresetSpec:
    if preset_id not in PRESETS:
        raise PresetNotFoundError(
            f"unknown preset: {preset_id!r}. Valid: {sorted(PRESETS)}"
        )
    return PRESETS[preset_id]


def expected_fourcc(preset: PresetSpec) -> str | None:
    """Return the codec_tag_string that a verified output for `preset` should have.
    `None` means no specific fourcc is required (e.g. audio-only or default tag is fine).
    """
    if preset.video_codec == "prores_ks":
        profile = str(preset.video_params.get("profile:v", ""))
        return PRORES_FOURCC.get(profile)
    if preset.video_codec == "libx265":
        return preset.container_flags.get("tag:v", "hvc1")  # hvc1 expected
    if preset.video_codec == "libx264":
        return None  # any default h264 tag is fine
    return None
