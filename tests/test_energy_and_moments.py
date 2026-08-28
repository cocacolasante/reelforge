"""CP4: per-second energy track + moment-anchored candidate generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.analysis.energy import _motion_track, loudness_deltas
from reelforge_core.models import (
    AnalysisReport,
    EnergyPoint,
    LoudnessPoint,
    SelectionConfig,
)
from reelforge_core.reels import generate_candidates
from reelforge_core.reels.generators.moment import (
    combined_scores,
    generate_moment_candidates,
    pick_peaks,
)

from tests.reels._fixtures import make_analysis


def _pt(i: int, motion: float, delta: float = 0.0) -> EnergyPoint:
    return EnergyPoint(time_sec=i + 0.5, motion=motion, loudness_delta=delta)


def _energy_analysis(asset_id, scene_durs, energy, loudness=None):
    analysis = make_analysis(asset_id, scene_durs)
    update = {"energy": energy}
    if loudness is not None:
        update["loudness"] = loudness
    return analysis.model_copy(update=update)


# ---- motion track (integration, real cv2 decode) ---------------------------


def test_motion_track_one_point_per_second_with_spikes_at_cuts(
    tmp_path_factory: pytest.TempPathFactory,
):
    pytest.importorskip("cv2")
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    # Luma-distinct colors (the shared multiscene fixture's red/green pair has
    # near-identical BT.601 luma, which a grey frame-diff is blind to).
    tmp = tmp_path_factory.mktemp("energy")
    parts = []
    for i, c in enumerate(["black", "white", "black"]):
        p = tmp / f"part{i}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=c={c}:s=320x240:d=2:r=25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p),
            ],
            check=True, capture_output=True,
        )
        parts.append(p)
    concat = tmp / "concat.txt"
    concat.write_text("\n".join(f"file '{p}'" for p in parts))
    out = tmp / "cuts.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(out)],
        check=True, capture_output=True,
    )
    motion = _motion_track(out, 6.0, sample_fps=2.0)
    assert len(motion) == 6
    # Solid-color interiors are still; the frame pairs straddling each cut
    # (t=2 and t=4) register a large diff in bins 2 and 4.
    assert motion[2] > 10.0
    assert motion[4] > 10.0
    assert motion[1] < 1.0
    assert motion[3] < 1.0
    assert motion[5] < 1.0


def test_motion_track_unreadable_source_is_zero_track(tmp_path: Path):
    pytest.importorskip("cv2")
    bogus = tmp_path / "nope.mp4"
    bogus.write_bytes(b"not a video")
    assert _motion_track(bogus, 3.0, sample_fps=2.0) == [0.0, 0.0, 0.0]


# ---- loudness deltas -------------------------------------------------------


def test_loudness_deltas_basic_and_first_bin_zero():
    pts = [
        LoudnessPoint(time_sec=0.5, lufs=-20.0),
        LoudnessPoint(time_sec=1.5, lufs=-14.0),
        LoudnessPoint(time_sec=2.5, lufs=-17.0),
    ]
    assert loudness_deltas(pts, 3) == [0.0, 6.0, -3.0]


def test_loudness_deltas_clamped_at_silence_sentinel():
    pts = [
        LoudnessPoint(time_sec=0.5, lufs=-80.0),
        LoudnessPoint(time_sec=1.5, lufs=-14.0),
        LoudnessPoint(time_sec=2.5, lufs=-80.0),
    ]
    # Deltas against the -80 sentinel are meaningless -> 0, not +66/-66.
    assert loudness_deltas(pts, 3) == [0.0, 0.0, 0.0]


def test_loudness_deltas_missing_bins_are_zero():
    pts = [LoudnessPoint(time_sec=0.5, lufs=-20.0), LoudnessPoint(time_sec=3.5, lufs=-15.0)]
    assert loudness_deltas(pts, 4) == [0.0, 0.0, 0.0, 0.0]


# ---- peak picking ----------------------------------------------------------


def test_pick_peaks_respects_min_separation():
    scored = [(float(t), 0.0) for t in range(40)]
    scored[10] = (10.0, 5.0)
    scored[14] = (14.0, 4.0)  # within 8s of the stronger peak at 10 -> dropped
    scored[30] = (30.0, 3.0)
    peaks = pick_peaks(scored, min_separation=8.0)
    assert peaks == [10.0, 30.0]


def test_pick_peaks_caps_count():
    scored = []
    for k in range(40):
        scored.append((k * 10.0, 5.0 + (k % 7)))
        scored.append((k * 10.0 + 5.0, 0.0))  # valleys so each is a local max
    assert len(pick_peaks(scored, min_separation=8.0, max_peaks=25)) == 25


# ---- moment candidate generation ------------------------------------------


def test_moment_windows_never_exceed_asset_bounds():
    # 40s asset, spike near t=5: every proposed window stays inside [0, 40].
    energy = [_pt(i, 1.0) for i in range(40)]
    energy[5] = _pt(5, 50.0)
    analysis = _energy_analysis("m1", [40.0], energy)
    cands = generate_moment_candidates(analysis, SelectionConfig())
    assert cands
    for c in cands:
        assert 0.0 <= c.start_sec < c.end_sec <= 40.0
        assert 30.0 <= c.duration_sec <= 60.0
        assert c.source == "moment"
        assert c.scene_indices == [0]


def test_moment_generator_flat_track_yields_nothing():
    energy = [_pt(i, 1.0) for i in range(40)]  # constant motion, no deltas
    analysis = _energy_analysis("m2", [40.0], energy)
    assert generate_moment_candidates(analysis, SelectionConfig()) == []


def test_moment_generator_no_energy_yields_nothing():
    analysis = make_analysis("m3", [40.0])
    assert analysis.energy == []
    assert generate_moment_candidates(analysis, SelectionConfig()) == []


def test_moment_candidates_join_union_with_source():
    energy = [_pt(i, 1.0) for i in range(60)]
    energy[20] = _pt(20, 80.0)
    analysis = _energy_analysis("m4", [90.0], energy)  # one 90s scene: no scene cands
    cands = generate_candidates(analysis, SelectionConfig())
    assert cands
    assert any(c.source == "moment" for c in cands)


def test_edge_snap_prefers_scene_cut_then_quiet_bin():
    # Scenes cut at 30.0; a peak-derived edge near it should land exactly on it.
    energy = [_pt(i, 1.0) for i in range(60)]
    energy[35] = _pt(35, 60.0)
    analysis = _energy_analysis("m5", [30.0, 30.0], energy)
    cands = generate_moment_candidates(analysis, SelectionConfig())
    assert cands
    # At least one window snapped an edge onto the scene cut at 30.0.
    assert any(c.start_sec == 30.0 or c.end_sec == 30.0 for c in cands)


def test_combined_scores_weighting():
    energy = [_pt(0, 0.0, 0.0), _pt(1, 10.0, 0.0), _pt(2, 0.0, 5.0)]
    analysis = _energy_analysis("m6", [3.0], energy)
    scored = dict(combined_scores(analysis))
    # Bin 1 is a pure-motion spike, bin 2 pure-loudness: motion carries the
    # larger weight (0.6 vs 0.4) over identically-shaped distributions.
    assert scored[1.5] > scored[2.5] > 0.0


# ---- schema compatibility --------------------------------------------------


def test_old_analysis_json_without_energy_still_parses():
    analysis = make_analysis("old", [10.0] * 3)
    payload = json.loads(analysis.model_dump_json())
    payload.pop("energy")
    rep = AnalysisReport.model_validate(payload)
    assert rep.energy == []
