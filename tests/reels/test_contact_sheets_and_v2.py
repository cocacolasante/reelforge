"""CP6: contact sheets + v2 multimodal message layout + listwise coercion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.models import SelectionConfig
from reelforge_core.reels import generate_candidates
from reelforge_core.reels.contact_sheet import (
    TILE_HEIGHT,
    build_contact_sheet_command,
    sheet_frame_times,
)
from reelforge_core.reels.rank import _coerce_rankings, rank

from tests.reels._fake_ranking_client import FakeRankingClient, all_rankings
from tests.reels._fixtures import make_analysis


# ---- frame times -----------------------------------------------------------


def test_sheet_frame_times_uses_peak_and_insets():
    first, mid, last = sheet_frame_times(10.0, 50.0, energy_peak_pos=0.25)
    assert first == 10.5
    assert mid == 20.0  # 10 + 0.25 * 40
    assert last == 49.5


def test_sheet_frame_times_midpoint_fallback_and_clamping():
    assert sheet_frame_times(10.0, 50.0) == [10.5, 30.0, 49.5]
    # Peak at the very start clamps inside the inset.
    first, mid, _ = sheet_frame_times(10.0, 50.0, energy_peak_pos=0.0)
    assert mid == first
    # Tiny span: insets shrink to dur/4.
    first, mid, last = sheet_frame_times(0.0, 1.0)
    assert first == 0.25 and last == 0.75


def test_build_contact_sheet_command_shape(tmp_path: Path):
    cmd = build_contact_sheet_command(Path("/src.mp4"), [1.0, 2.0, 3.0], tmp_path / "o.jpg")
    assert cmd.count("-ss") == 3 and cmd.count("-i") == 3
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert f"scale=-2:{TILE_HEIGHT}" in fc and "hstack=inputs=3" in fc
    assert cmd[cmd.index("-frames:v") + 1] == "1"


def test_contact_sheet_extraction_integration(multiscene_mp4: Path, tmp_path: Path):
    cv2 = pytest.importorskip("cv2")
    from reelforge_core.compose.graph import run_ffmpeg

    out = tmp_path / "sheet.jpg"
    cmd = build_contact_sheet_command(multiscene_mp4, [0.5, 3.0, 5.5], out)
    run_ffmpeg(cmd, timeout_sec=60)
    img = cv2.imread(str(out))
    assert img is not None
    h, w = img.shape[:2]
    assert h == TILE_HEIGHT
    # Three 320x240 tiles scaled to 180 high -> 240 wide each -> 720 total.
    assert w == 720


# ---- v2 message layout -----------------------------------------------------


async def test_rank_sends_one_image_block_per_candidate(tmp_path: Path):
    analysis = make_analysis("img1", [10.0] * 6)
    cfg = SelectionConfig()
    cands = generate_candidates(analysis, cfg)[:4]
    sheets = {}
    for c in cands:
        p = tmp_path / f"{c.candidate_id}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0fakejpg")
        sheets[c.candidate_id] = p
    client = FakeRankingClient(script=[])
    await rank(cands, analysis, cfg, client=client, sheets=sheets)

    blocks = client.calls[0]["messages"][0]["content"]
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    assert len(image_blocks) == len(cands)
    assert len(text_blocks) == len(cands) + 1  # intro + one JSON per candidate
    src = image_blocks[0]["source"]
    assert src["type"] == "base64" and src["media_type"] == "image/jpeg"
    # Interleaving: each image is immediately followed by its candidate JSON.
    for i, b in enumerate(blocks):
        if b.get("type") == "image":
            assert blocks[i + 1]["type"] == "text"
            assert "candidate_id" in json.loads(blocks[i + 1]["text"])


async def test_rank_without_sheets_is_text_only():
    analysis = make_analysis("img2", [10.0] * 6)
    cfg = SelectionConfig()
    cands = generate_candidates(analysis, cfg)[:3]
    client = FakeRankingClient(script=[])
    await rank(cands, analysis, cfg, client=client)
    blocks = client.calls[0]["messages"][0]["content"]
    assert not [b for b in blocks if b.get("type") == "image"]
    assert len([b for b in blocks if b.get("type") == "text"]) == len(cands) + 1


# ---- listwise coercion -----------------------------------------------------


def test_coerce_stores_rank_position_and_opening_description():
    analysis = make_analysis("co1", [10.0] * 5)
    cands = generate_candidates(analysis, SelectionConfig())
    cmap = {c.candidate_id: c for c in cands}
    rankings = all_rankings([c.candidate_id for c in cands])
    reels = _coerce_rankings(rankings, candidate_map=cmap)
    by_id = {r.candidate_id: r for r in reels}
    for i, c in enumerate(cands):
        assert by_id[c.candidate_id].rank_position == i + 1
        assert by_id[c.candidate_id].opening_description.startswith("Opening frame")


def test_coerce_rank_position_breaks_overall_ties():
    analysis = make_analysis("co2", [10.0] * 5)
    cands = generate_candidates(analysis, SelectionConfig())[:2]
    cmap = {c.candidate_id: c for c in cands}
    same_scores = {
        "narrative_coherence": 70,
        "hook_strength": 70,
        "emotional_payoff": 70,
        "standalone_clarity": 70,
    }
    rankings = []
    for cid, pos in ((cands[0].candidate_id, 2), (cands[1].candidate_id, 1)):
        rankings.append(
            {
                "candidate_id": cid,
                "title": f"t {cid}",
                "hook": "h",
                "justification": "j",
                "suggested_mood": "neutral",
                "scores": same_scores,
                "rank_position": pos,
                "opening_description": "opening",
            }
        )
    reels = _coerce_rankings(rankings, candidate_map=cmap)
    # Equal overall -> the model's explicit order wins: position 1 first.
    assert reels[0].candidate_id == cands[1].candidate_id
    assert reels[0].rank_position == 1


def test_coerce_tolerates_missing_v2_fields():
    """Old ranking_raw.json files (v1) coerce fine — fields default to None."""
    analysis = make_analysis("co3", [10.0] * 5)
    cands = generate_candidates(analysis, SelectionConfig())[:1]
    cmap = {c.candidate_id: c for c in cands}
    entry = all_rankings([cands[0].candidate_id])[0]
    del entry["rank_position"]
    del entry["opening_description"]
    reels = _coerce_rankings([entry], candidate_map=cmap)
    assert reels[0].rank_position is None
    assert reels[0].opening_description is None
