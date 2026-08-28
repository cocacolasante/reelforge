"""Music-credit derivation + idempotent append for published reels."""

from __future__ import annotations

import json
from pathlib import Path

from reelforge_core.publish.credits import append_credit, music_credit_for_reel


def _write_manifest(tmp_path: Path, chosen_music) -> tuple[str, str, Path]:
    aid, rid = "a" * 64, "reel1"
    d = tmp_path / "working" / aid / "reels" / rid
    d.mkdir(parents=True)
    (d / "compose.json").write_text(json.dumps({"chosen_music": chosen_music}))
    return aid, rid, tmp_path


def test_ccby_track_yields_credit(tmp_path: Path) -> None:
    aid, rid, dd = _write_manifest(
        tmp_path,
        {"license": "CC-BY-4.0",
         "attribution": "Music: 'Jul' by Scott Buckley — released under CC-BY 4.0. www.scottbuckley.com.au"},
    )
    credit = music_credit_for_reel(aid, rid, dd)
    assert credit is not None and "Scott Buckley" in credit
    assert credit.startswith("Music:")
    assert credit.count("Music:") == 1  # no double prefix


def test_attribution_without_music_prefix_gets_one(tmp_path: Path) -> None:
    aid, rid, dd = _write_manifest(
        tmp_path, {"license": "CC-BY", "attribution": "'X' by Someone"}
    )
    assert music_credit_for_reel(aid, rid, dd) == "Music: 'X' by Someone"


def test_cc0_track_yields_no_credit(tmp_path: Path) -> None:
    aid, rid, dd = _write_manifest(
        tmp_path, {"license": "CC0", "attribution": "'Beach' by Loyalty Freak Music (CC0)"}
    )
    assert music_credit_for_reel(aid, rid, dd) is None


def test_no_music_or_missing_manifest_yields_none(tmp_path: Path) -> None:
    aid, rid, dd = _write_manifest(tmp_path, None)
    assert music_credit_for_reel(aid, rid, dd) is None
    assert music_credit_for_reel("b" * 64, "nope", dd) is None


def test_append_credit_idempotent() -> None:
    credit = "Music: 'Jul' by Scott Buckley"
    out = append_credit("My sick edit", credit)
    assert out == "My sick edit\n\nMusic: 'Jul' by Scott Buckley"
    assert append_credit(out, credit) == out  # already present → unchanged
    assert append_credit("", credit) == credit
    assert append_credit("desc", None) == "desc"
