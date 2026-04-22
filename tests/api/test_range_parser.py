from __future__ import annotations

import pytest

from apps.api.streaming import parse_range


@pytest.mark.parametrize(
    "header,size,expected",
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=500-", 1000, (500, 999)),
        ("bytes=-100", 1000, (900, 999)),
        ("bytes=0-", 500, (0, 499)),
        ("bytes=-500", 300, (0, 299)),  # suffix larger than file → clamped
    ],
)
def test_range_parses(header: str, size: int, expected: tuple[int, int]) -> None:
    assert parse_range(header, size) == expected


@pytest.mark.parametrize(
    "header,size",
    [
        ("bytes=1000-2000", 500),  # start past EOF
        ("bytes=500-100", 1000),   # end before start
        ("bytes=", 1000),           # no range
        ("garbage", 1000),
        ("bytes=abc-def", 1000),
    ],
)
def test_range_unsatisfiable(header: str, size: int) -> None:
    assert parse_range(header, size) == (None, None)
