from __future__ import annotations

import pytest

from reelforge_core.errors import PresetNotFoundError
from reelforge_core.export import PRESET_SPEC_VERSION, PRESETS, get_preset
from reelforge_core.export.presets import expected_fourcc


def test_preset_registry_has_four() -> None:
    assert len(PRESETS) == 4


def test_preset_ids_are_unique() -> None:
    ids = [p.id for p in PRESETS.values()]
    assert len(set(ids)) == len(ids)


def test_preset_spec_version_is_versioned_string() -> None:
    assert isinstance(PRESET_SPEC_VERSION, str) and PRESET_SPEC_VERSION


def test_prores_profiles_differ() -> None:
    # REGRESSION GUARD for the spec's intentional-bug planting.
    # 422 Standard → profile 2, 422 HQ → profile 3.
    assert PRESETS["mov_prores_422"].video_params["profile:v"] == "2"
    assert PRESETS["mov_prores_hq"].video_params["profile:v"] == "3"


def test_prores_uses_422_pixel_format() -> None:
    for pid in ("mov_prores_422", "mov_prores_hq"):
        assert PRESETS[pid].video_pixel_format == "yuv422p10le"
        assert PRESETS[pid].video_codec == "prores_ks"
        assert PRESETS[pid].video_params.get("vendor") == "apl0"


def test_h265_has_hvc1_tag() -> None:
    assert PRESETS["mp4_h265_hq"].container_flags.get("tag:v") == "hvc1"


def test_h264_has_faststart() -> None:
    assert PRESETS["mp4_h264_social"].container_flags.get("movflags") == "+faststart"


def test_h264_profile_level() -> None:
    p = PRESETS["mp4_h264_social"].video_params
    assert p.get("profile:v") == "high"
    assert str(p.get("level")) == "4.1"


def test_expected_fourcc_prores() -> None:
    assert expected_fourcc(PRESETS["mov_prores_422"]) == "apcn"
    assert expected_fourcc(PRESETS["mov_prores_hq"]) == "apch"


def test_expected_fourcc_h265_is_hvc1() -> None:
    assert expected_fourcc(PRESETS["mp4_h265_hq"]) == "hvc1"


def test_expected_fourcc_h264_is_unconstrained() -> None:
    assert expected_fourcc(PRESETS["mp4_h264_social"]) is None


def test_unknown_preset_raises() -> None:
    with pytest.raises(PresetNotFoundError):
        get_preset("mp4_h264_totally_not_real")
