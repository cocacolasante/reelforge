"""Fetch curated royalty-free music packs into the user music library.

Two packs:
  1. Scott Buckley (scottbuckley.com.au) — cinematic/ambient/electronic,
     CC-BY 4.0. Attribution is REQUIRED; ReelForge auto-appends the credit
     line to YouTube descriptions and IG/TikTok captions at publish time.
     Track pages are scraped for the current direct MP3 URL at run time so
     the script survives his upload-path changes.
  2. Loyalty Freak Music — lo-fi/chill/beats, CC0 (public domain), mirrored
     on the Internet Archive (license verified per item via the IA metadata
     API). No attribution required; recorded anyway for provenance.

Run inside any ReelForge container (needs network + ffprobe + /data mounted):

    docker compose run --rm -T --entrypoint python cli - < scripts/fetch_music_packs.py

Idempotent: re-running refreshes these entries and leaves other user tracks
alone. It also REMOVES the old OpenGameArt pack (ids containing "-oga-") —
those game-soundtrack placeholders are what these packs replace.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

MUSIC_DIR = Path("/data/music")

SB_CREDIT = (
    "Music: '{title}' by Scott Buckley — released under CC-BY 4.0. "
    "www.scottbuckley.com.au"
)

# (id, mood, slug, title)
SCOTT_BUCKLEY: list[tuple[str, str, str, str]] = [
    ("sb-jul", "calm", "jul", "Jul"),
    ("sb-aurora", "calm", "aurora", "Aurora"),
    ("sb-moonlight", "romantic", "moonlight", "Moonlight"),
    ("sb-undertow", "tense", "undertow", "Undertow"),
    ("sb-thelongdark", "somber", "the-long-dark", "The Long Dark"),
    ("sb-chasingdaylight", "triumphant", "chasing-daylight", "Chasing Daylight"),
    ("sb-iwalkwithghosts", "melancholic", "i-walk-with-ghosts", "I Walk With Ghosts"),
    ("sb-hiraeth", "melancholic", "hiraeth", "Hiraeth"),
    ("sb-helios", "mysterious", "helios", "Helios"),
    ("sb-phaseshift", "energetic", "phase-shift", "Phase Shift"),
    ("sb-goldenhour", "joyful", "golden-hour", "Golden Hour"),
    ("sb-solace", "neutral", "solace", "Solace"),
]

# (id, mood, ia_identifier, filename, title)
LOYALTY_FREAK: list[tuple[str, str, str, str, str]] = [
    ("lfm-ihopeyourehappy", "calm", "lofi-ambient-songs",
     "I hope you're happy.mp3", "I Hope You're Happy"),
    ("lfm-thispainissoft", "melancholic", "lofi-ambient-songs",
     "This pain is soft but here.mp3", "This Pain Is Soft But Here"),
    ("lfm-cryingthosetears", "somber", "lofi-ambient-songs",
     "Crying those tears I've kept so long.mp3", "Crying Those Tears"),
    ("lfm-sugarandcoffee", "joyful", "lofi-ambient-songs",
     "Lack of Color - Sugar and coffee.mp3", "Sugar and Coffee"),
    ("lfm-aeroplane", "neutral", "lofi-ambient-songs",
     "Lack of Color - Aeroplane.mp3", "Aeroplane"),
    ("lfm-oncemorewithyou", "neutral", "MINIMALAMBIENTBOUNCE",
     "Loyalty Freak Music - MINIMAL AMBIENT BOUNCE - 01 Once more with you.mp3",
     "Once More With You"),
    ("lfm-onecoolminute", "energetic", "MINIMALAMBIENTBOUNCE",
     "Loyalty Freak Music - MINIMAL AMBIENT BOUNCE - 02 One Cool Minute.mp3",
     "One Cool Minute"),
    ("lfm-lag", "mysterious", "MINIMALAMBIENTBOUNCE",
     "Loyalty Freak Music - MINIMAL AMBIENT BOUNCE - 06 Lag.mp3", "Lag"),
    ("lfm-beach", "joyful", "LoyaltyFreakMusicMELODIESWITHABEAT2018030251947223",
     "Loyalty_Freak_Music_-_08_-_Beach.mp3", "Beach"),
    ("lfm-work", "energetic", "LoyaltyFreakMusicMELODIESWITHABEAT2018030251947223",
     "Loyalty_Freak_Music_-_13_-_Work.mp3", "Work"),
    ("lfm-loveher", "romantic", "LoyaltyFreakMusicMELODIESWITHABEAT2018030251947223",
     "Loyalty_Freak_Music_-_04_-_Love_Her.mp3", "Love Her"),
    ("lfm-standing", "triumphant", "LoyaltyFreakMusicMELODIESWITHABEAT2018030251947223",
     "Loyalty_Freak_Music_-_11_-_Standing.mp3", "Standing"),
]

UA = "Mozilla/5.0 (ReelForge music fetcher; one-time curated download)"


def _curl(url: str, out: Path | None = None) -> str:
    args = ["curl", "-sL", "--fail", "--max-time", "180", "-A", UA, url]
    if out is not None:
        subprocess.run(args + ["-o", str(out)], check=True)
        return ""
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return round(float(out.stdout.strip()), 3)


def scrape_sb_mp3(slug: str) -> str:
    html = _curl(f"https://www.scottbuckley.com.au/library/{slug}/")
    m = re.search(r'https://[^"\']+\.mp3', html)
    if not m:
        raise RuntimeError(f"no mp3 link found on scottbuckley.com.au/library/{slug}/")
    return m.group(0)


def verify_ia_cc0(identifier: str) -> None:
    meta = json.loads(_curl(f"https://archive.org/metadata/{identifier}"))
    lic = meta.get("metadata", {}).get("licenseurl", "")
    if "publicdomain/zero" not in lic:
        raise RuntimeError(f"IA item {identifier} is not CC0 (licenseurl={lic!r})")


def merge_manifest(entries: list[dict]) -> None:
    manifest_path = MUSIC_DIR / "manifest.json"
    manifest = {"tracks": []}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    new_ids = {e["id"] for e in entries}
    kept = []
    for t in manifest.get("tracks", []):
        if t["id"] in new_ids:
            continue  # refreshed below
        if "-oga-" in t["id"]:  # retire the OpenGameArt placeholder pack
            Path(t["path"]).unlink(missing_ok=True)
            print(f"  retired {t['id']}")
            continue
        kept.append(t)
    manifest["tracks"] = kept + entries
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {len(manifest['tracks'])} tracks total")


def main() -> int:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    print("== Scott Buckley (CC-BY 4.0) ==")
    for tid, mood, slug, title in SCOTT_BUCKLEY:
        dest = MUSIC_DIR / f"{tid}.mp3"
        try:
            if not dest.exists() or dest.stat().st_size < 100_000:
                url = scrape_sb_mp3(slug)
                _curl(url, out=dest)
            entries.append({
                "id": tid, "path": str(dest), "bpm": None, "mood": mood,
                "duration_sec": ffprobe_duration(dest),
                "license": "CC-BY-4.0",
                "attribution": SB_CREDIT.format(title=title),
            })
            print(f"  ok {tid} ({mood}) {entries[-1]['duration_sec']}s")
        except Exception as exc:
            print(f"  SKIP {tid}: {exc}")

    print("== Loyalty Freak Music (CC0, via Internet Archive) ==")
    verified: set[str] = set()
    for tid, mood, ident, fname, title in LOYALTY_FREAK:
        dest = MUSIC_DIR / f"{tid}.mp3"
        try:
            if ident not in verified:
                verify_ia_cc0(ident)
                verified.add(ident)
            if not dest.exists() or dest.stat().st_size < 100_000:
                url = f"https://archive.org/download/{ident}/{urllib.parse.quote(fname)}"
                _curl(url, out=dest)
            entries.append({
                "id": tid, "path": str(dest), "bpm": None, "mood": mood,
                "duration_sec": ffprobe_duration(dest),
                "license": "CC0",
                "attribution": f"'{title}' by Loyalty Freak Music (CC0)",
            })
            print(f"  ok {tid} ({mood}) {entries[-1]['duration_sec']}s")
        except Exception as exc:
            print(f"  SKIP {tid}: {exc}")

    if not entries:
        print("nothing fetched; manifest untouched")
        return 1
    merge_manifest(entries)
    moods = sorted({e["mood"] for e in entries})
    print(f"fetched {len(entries)} tracks covering moods: {', '.join(moods)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
