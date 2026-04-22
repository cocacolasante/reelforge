from __future__ import annotations

from reelforge_core.db import semantics_cache_key


def _args(**overrides):
    base = dict(
        asset_id="abc",
        scene_index=2,
        model="claude-haiku-4-5-20251001",
        prompt_version="v1",
        thumb_sha256="aa" * 32,
        transcript_slice_sha256="bb" * 32,
    )
    base.update(overrides)
    return base


def test_cache_key_is_stable() -> None:
    a = semantics_cache_key(**_args())
    b = semantics_cache_key(**_args())
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_any_field_change_changes_key() -> None:
    base = semantics_cache_key(**_args())
    variants = [
        _args(asset_id="different"),
        _args(scene_index=3),
        _args(model="claude-sonnet-4-5"),
        _args(prompt_version="v2"),
        _args(thumb_sha256="cc" * 32),
        _args(transcript_slice_sha256="dd" * 32),
    ]
    for v in variants:
        assert semantics_cache_key(**v) != base
