"""CP6: contact sheets + v2 multimodal message layout + listwise coercion.

Action-aware cuts CP2: sheets carry red-bordered frames OUTSIDE the span, edge
strips for refinement, and the ranker sees words/events just past each edge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reelforge_core.models import ReelCandidate, SelectionConfig
from reelforge_core.reels import generate_candidates
from reelforge_core.reels.contact_sheet import (
    BLANK_TILE_WIDTH,
    SHEET_OUTSIDE,
    TILE_HEIGHT,
    build_contact_sheet_command,
    edge_strip_times,
    sheet_frame_times,
)
from reelforge_core.reels.events import ActionEvent
from reelforge_core.reels.rank import (
    _coerce_rankings,
    build_candidate_context,
    build_system_prompt,
    rank,
)

from tests.reels._fake_ranking_client import FakeRankingClient, all_rankings
from tests.reels._fixtures import make_analysis


# ---- frame times -----------------------------------------------------------


def test_sheet_frame_times_uses_peak_insets_and_outside_frames():
    before, first, mid, last, after = sheet_frame_times(10.0, 50.0, energy_peak_pos=0.25)
    assert (before, first, last, after) == (8.0, 10.5, 49.5, 52.0)
    assert mid == 20.0  # 10 + 0.25 * 40


def test_sheet_frame_times_midpoint_fallback_and_clamping():
    assert sheet_frame_times(10.0, 50.0) == [8.0, 10.5, 30.0, 49.5, 52.0]
    # Peak at the very start clamps inside the inset.
    _, first, mid, _, _ = sheet_frame_times(10.0, 50.0, energy_peak_pos=0.0)
    assert mid == first
    # Tiny span: insets shrink to dur/4; nothing exists before t=0.
    before, first, _, last, _ = sheet_frame_times(0.0, 1.0)
    assert before is None and first == 0.25 and last == 0.75


def test_outside_frames_past_the_footage_edge_are_none():
    before, *_, after = sheet_frame_times(1.0, 30.0, duration=31.0)
    assert before is None and after is None


def test_edge_strip_times():
    assert edge_strip_times(10.0, 40.0, 100.0) == [7.0, 8.5, 10.1, 11.5, 38.5, 39.9, 41.5, 43.0]
    assert edge_strip_times(1.0, 40.0, 41.0) == [None, None, 1.1, 2.5, 38.5, 39.9, None, None]


def test_build_contact_sheet_command_shape(tmp_path: Path):
    cmd = build_contact_sheet_command(Path("/src.mp4"), [1.0, 2.0, 3.0], tmp_path / "o.jpg")
    assert cmd.count("-ss") == 3 and cmd.count("-i") == 3
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert f"scale=-2:{TILE_HEIGHT}" in fc and "hstack=inputs=3" in fc
    assert "drawbox" not in fc
    assert cmd[cmd.index("-frames:v") + 1] == "1"


def test_command_borders_outside_tiles_and_blanks_missing_times(tmp_path: Path):
    cmd = build_contact_sheet_command(
        Path("/src.mp4"), [None, 1.0, 2.0, 3.0, 4.0], tmp_path / "o.jpg", outside=SHEET_OUTSIDE
    )
    assert cmd.count("-ss") == 4 and "lavfi" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("drawbox") == 2 and "hstack=inputs=5" in fc
    assert "[0:v]scale=-2:180,format=yuv420p,drawbox" in fc
    assert "[4:v]scale=-2:180,format=yuv420p,drawbox" in fc


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


def test_five_tile_sheet_integration_with_blank_and_borders(multiscene_mp4: Path, tmp_path: Path):
    cv2 = pytest.importorskip("cv2")
    from reelforge_core.compose.graph import run_ffmpeg

    out = tmp_path / "sheet5.jpg"
    cmd = build_contact_sheet_command(
        multiscene_mp4, [None, 0.5, 3.0, 5.5, 5.9], out, outside=SHEET_OUTSIDE
    )
    run_ffmpeg(cmd, timeout_sec=60)
    img = cv2.imread(str(out))
    h, w = img.shape[:2]
    assert h == TILE_HEIGHT
    assert w == BLANK_TILE_WIDTH + 4 * 240
    # The blank "before" tile is outside the span: red border on black.
    b, g, r = (int(v) for v in img[TILE_HEIGHT // 2, 3])
    assert r > 150 and g < 100 and b < 100
    b, g, r = (int(v) for v in img[TILE_HEIGHT // 2, BLANK_TILE_WIDTH // 2])
    assert r < 60 and g < 60 and b < 60


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
            ctx = json.loads(blocks[i + 1]["text"])
            assert "candidate_id" in ctx
            assert {"words_before_start", "words_after_end", "action_events"} <= set(ctx)


async def test_rank_without_sheets_is_text_only():
    analysis = make_analysis("img2", [10.0] * 6)
    cfg = SelectionConfig()
    cands = generate_candidates(analysis, cfg)[:3]
    client = FakeRankingClient(script=[])
    await rank(cands, analysis, cfg, client=client)
    blocks = client.calls[0]["messages"][0]["content"]
    assert not [b for b in blocks if b.get("type") == "image"]
    assert len([b for b in blocks if b.get("type") == "text"]) == len(cands) + 1


def test_candidate_context_shows_what_the_edges_leave_out():
    # make_analysis puts one word " sceneN" at each 10s scene start.
    analysis = make_analysis("ctx", [10.0] * 6)
    cand = ReelCandidate(
        candidate_id="c",
        scene_indices=[1, 2, 3],
        start_sec=15.0,
        end_sec=38.0,
        duration_sec=23.0,
        scene_count=3,
        source="scene",
    )
    events = [
        ActionEvent(start_sec=40.0, end_sec=42.0, peak_sec=41.0, strength=5.0),
        ActionEvent(start_sec=16.0, end_sec=18.0, peak_sec=17.0, strength=4.0),
    ]
    ctx = build_candidate_context(cand, analysis, events=events)
    assert ctx["words_before_start"] == [[10.0, "scene1"]]
    assert ctx["words_after_end"] == [[40.0, "scene4"]]
    assert [e["where"] for e in ctx["action_events"]] == ["after_end", "inside"]


def test_ranking_prompt_v4_explains_outside_frames_and_announced_action():
    assert SelectionConfig().ranking_prompt_version == "v4"
    prompt = build_system_prompt(SelectionConfig())
    assert "RED BORDER" in prompt and "another one coming" in prompt


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
        assert by_id[c.candidate_id].edit_style is not None


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
    del entry["content_style"]
    reels = _coerce_rankings([entry], candidate_map=cmap)
    assert reels[0].rank_position is None
    assert reels[0].opening_description is None
    assert reels[0].edit_style is None


def test_coerce_rejects_bogus_content_style_without_dropping_entry():
    analysis = make_analysis("co4", [10.0] * 5)
    cands = generate_candidates(analysis, SelectionConfig())[:1]
    cmap = {c.candidate_id: c for c in cands}
    entry = all_rankings([cands[0].candidate_id])[0]
    entry["content_style"] = "vaporwave"
    reels = _coerce_rankings([entry], candidate_map=cmap)
    assert len(reels) == 1 and reels[0].edit_style is None
