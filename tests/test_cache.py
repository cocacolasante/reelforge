from __future__ import annotations

from pathlib import Path

import pytest

from reelforge_core import cache


@pytest.fixture
def cache_env(isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    if hasattr(cache._LOCAL, "conn"):
        try:
            cache._LOCAL.conn.close()
        except Exception:
            pass
        del cache._LOCAL.conn
    monkeypatch.setenv("REELFORGE_DATA_DIR", str(isolated_data_dir))
    return isolated_data_dir


def test_compute_key_is_deterministic(cache_env: Path) -> None:
    a = cache.compute_key("clip", {"b": 1, "a": "x"})
    b = cache.compute_key("clip", {"a": "x", "b": 1})  # key order shouldn't matter
    assert a == b


def test_compute_key_differs_for_different_inputs(cache_env: Path) -> None:
    a = cache.compute_key("clip", {"a": "1"})
    b = cache.compute_key("clip", {"a": "2"})
    assert a != b
    assert cache.compute_key("clip", {"a": "1"}) != cache.compute_key(
        "music", {"a": "1"}
    )


def test_lookup_missing_returns_none(cache_env: Path) -> None:
    assert cache.lookup("does-not-exist") is None


def test_register_and_lookup(cache_env: Path) -> None:
    key = cache.compute_key("clip", {"k": 1})
    p = cache.path_for("clip", key, "mp4")
    p.write_bytes(b"x" * 10)
    cache.register(key, "clip", p)
    got = cache.lookup(key)
    assert got == p


def test_lookup_cleans_up_missing_file(cache_env: Path) -> None:
    key = cache.compute_key("clip", {"k": 2})
    p = cache.path_for("clip", key, "mp4")
    p.write_bytes(b"x" * 10)
    cache.register(key, "clip", p)
    p.unlink()
    # Missing file → lookup returns None AND the row is removed
    assert cache.lookup(key) is None
    # Sanity: re-register should succeed afterwards
    p.write_bytes(b"y" * 10)
    cache.register(key, "clip", p)
    assert cache.lookup(key) == p


def test_evict_lru_removes_oldest(cache_env: Path) -> None:
    # Seed three entries with increasing size so total > cap easily.
    keys = []
    for i in range(3):
        k = cache.compute_key("clip", {"i": i})
        p = cache.path_for("clip", k, "mp4")
        p.write_bytes(b"x" * (1024 * (i + 1)))
        cache.register(k, "clip", p)
        keys.append(k)
    # Cap at ~2 KiB — should evict the oldest two.
    cap = 2 * 1024 + 100  # a bit under the two largest combined
    freed = cache.evict_if_over_cap("clip", cap)
    assert freed > 0
    # The oldest-inserted should be gone
    assert cache.lookup(keys[0]) is None


def test_cache_size_sum(cache_env: Path) -> None:
    for i in range(3):
        k = cache.compute_key("music", {"i": i})
        p = cache.path_for("music", k, "wav")
        p.write_bytes(b"x" * 100)
        cache.register(k, "music", p)
    assert cache.cache_size("music") == 300
    # Wrong kind returns 0
    assert cache.cache_size("clip") == 0


def test_purge_kind(cache_env: Path) -> None:
    k = cache.compute_key("clip", {"k": "purge"})
    p = cache.path_for("clip", k, "mp4")
    p.write_bytes(b"z" * 50)
    cache.register(k, "clip", p)
    freed = cache.purge_kind("clip")
    assert freed == 50
    assert cache.lookup(k) is None
    assert not p.exists()


def test_cap_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_CLIPS_GB", "5")
    assert cache.cap_from_env("clip", 20.0) == 5 * 1024**3
    monkeypatch.delenv("CACHE_CLIPS_GB")
    assert cache.cap_from_env("clip", 20.0) == 20 * 1024**3
    # Unknown kind returns the default
    assert cache.cap_from_env("bogus", 1.5) == int(1.5 * 1024**3)
    # Invalid value falls back to default
    monkeypatch.setenv("CACHE_CLIPS_GB", "not-a-number")
    assert cache.cap_from_env("clip", 7.0) == 7 * 1024**3
