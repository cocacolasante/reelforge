from __future__ import annotations

from reelforge_core.analysis.audio import bin_loudness, parse_ebur128_stderr


SAMPLE = """\
[Parsed_ebur128_0 @ 0x5e0] t: 0.100   M: -inf  S: -inf  I: -inf LUFS LRA: 0.0 LU
[Parsed_ebur128_0 @ 0x5e0] t: 0.500   M: -30.2  S: -inf  I: -inf LUFS LRA: 0.0 LU
[Parsed_ebur128_0 @ 0x5e0] t: 1.200   M: -18.4  S: -22.0 I: -22.0 LUFS LRA: 0.5 LU
[Parsed_ebur128_0 @ 0x5e0] t: 1.700   M: -16.8  S: -22.0 I: -22.0 LUFS LRA: 0.5 LU
[Parsed_ebur128_0 @ 0x5e0] t: 2.300   M: -12.1  S: -19.0 I: -19.0 LUFS LRA: 0.8 LU
some noise line that should be ignored
[Parsed_ebur128_0 @ 0x5e0] t: 2.800   M: -inf   S: -inf  I: -inf LUFS LRA: 0.0 LU
"""


def test_parser_extracts_all_samples_including_neg_inf() -> None:
    samples = parse_ebur128_stderr(SAMPLE.splitlines())
    # 6 lines match the regex; "some noise line" must not.
    assert len(samples) == 6
    # -inf serialized as -80.0
    assert samples[0][1] == -80.0
    assert samples[0][0] == 0.1
    assert samples[-1][1] == -80.0


def test_bins_have_1_second_centers() -> None:
    samples = parse_ebur128_stderr(SAMPLE.splitlines())
    points = bin_loudness(samples, duration_sec=3.0)
    # bins for 0, 1, 2
    assert [p.time_sec for p in points] == [0.5, 1.5, 2.5]
    # bin 0 has two samples including -80, so mean is (-80 + -30.2) / 2
    assert points[0].lufs < -30


def test_empty_duration_produces_empty_list() -> None:
    assert bin_loudness([], 0.0) == []


def test_out_of_order_timestamps_still_bin_correctly() -> None:
    samples = [(2.5, -10.0), (0.3, -40.0), (1.1, -20.0)]
    points = bin_loudness(samples, duration_sec=3.0)
    assert points[0].lufs == -40.0
    assert points[1].lufs == -20.0
    assert points[2].lufs == -10.0


def test_empty_bin_becomes_neg80() -> None:
    samples = [(0.3, -20.0)]  # nothing in bin 1
    points = bin_loudness(samples, duration_sec=2.0)
    assert points[0].lufs == -20.0
    assert points[1].lufs == -80.0
