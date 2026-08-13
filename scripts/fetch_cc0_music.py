"""Fetch a curated set of CC0 music from OpenGameArt into the user music library.

Run inside any ReelForge container (needs network + ffprobe + /data mounted):

    docker compose run --rm cli python /data/../app/scripts/fetch_cc0_music.py
    # or pipe it:  docker compose run --rm -T cli python - < scripts/fetch_cc0_music.py

Every track below is published under CC0 (verified on its OpenGameArt page).
Attribution is recorded in the manifest anyway — it's good manners, and it
gives the user provenance if they ever need to prove license status.

The script is idempotent: re-running refreshes these entries and leaves any
other user-uploaded tracks in the manifest untouched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

MUSIC_DIR = Path("/data/music")

# (id, mood, bpm, title, author, url)
TRACKS: list[tuple[str, str, int | None, str, str, str]] = [
    ("calm-oga-crystalcave", "calm", None, "Crystal Cave (song18)", "cynicmusic",
     "https://opengameart.org/sites/default/files/song18_0.mp3"),
    ("neutral-oga-town", "neutral", None, "Town Theme RPG", "cynicmusic",
     "https://opengameart.org/sites/default/files/TownTheme.mp3"),
    ("joyful-oga-bossa", "joyful", 129, "8bit Bossa", "Joth",
     "https://opengameart.org/sites/default/files/8bit%20Bossa.mp3"),
    ("energetic-oga-fight", "energetic", None, "Fast Fight / Battle Music", "Ville Nousiainen",
     "https://opengameart.org/sites/default/files/fight.ogg"),
    ("tense-oga-cyberpunk", "tense", 108, "Cyberpunk Moonlight Sonata", "Joth",
     "https://opengameart.org/sites/default/files/Cyberpunk%20Moonlight%20Sonata_0.mp3"),
    ("mysterious-oga-song21", "mysterious", None, "Mysterious Ambience (song21)", "cynicmusic",
     "https://opengameart.org/sites/default/files/song21_0.mp3"),
    ("mysterious-oga-forest", "mysterious", None, "Creepy Forest", "Brandon Morris",
     "https://opengameart.org/sites/default/files/forest.ogg"),
    ("somber-oga-tragic", "somber", None, "Tragic Ambient Main Menu", "brandon75689",
     "https://opengameart.org/sites/default/files/ambientmain_0.ogg"),
    ("romantic-oga-nexttoyou", "romantic", None, "Next to You", "Joth",
     "https://opengameart.org/sites/default/files/Next%20to%20You.mp3"),
    ("melancholic-oga-snowfall", "melancholic", None, "Snowfall", "Kistol",
     "https://opengameart.org/sites/default/files/Snowfall_0.ogg"),
    ("triumphant-oga-fieldofdreams", "triumphant", None, "The Field Of Dreams", "pauliuw",
     "https://opengameart.org/sites/default/files/the_field_of_dreams.mp3"),
]


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return round(float(out.stdout.strip()), 3)


def main() -> int:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MUSIC_DIR / "manifest.json"
    existing: list[dict] = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text())["tracks"]
        except Exception:
            print("warning: existing manifest unreadable; starting fresh", file=sys.stderr)
    our_ids = {t[0] for t in TRACKS}
    kept = [e for e in existing if e.get("id") not in our_ids]

    entries: list[dict] = []
    for track_id, mood, bpm, title, author, url in TRACKS:
        ext = url.rsplit(".", 1)[-1].lower()
        dest = MUSIC_DIR / f"{track_id}.{ext}"
        if not dest.exists() or dest.stat().st_size == 0:
            print(f"downloading {track_id} <- {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "reelforge-music-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
                f.write(resp.read())
        else:
            print(f"exists      {track_id}")
        duration = ffprobe_duration(dest)
        entries.append(
            {
                "id": track_id,
                "path": str(dest),
                "bpm": bpm,
                "mood": mood,
                "duration_sec": duration,
                "license": "CC0",
                "attribution": f"\"{title}\" by {author} (OpenGameArt.org, CC0)",
            }
        )
        print(f"   ok       {duration:>7.1f}s  {mood}")

    manifest = {"tracks": kept + entries}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nwrote {manifest_path} with {len(kept) + len(entries)} tracks ({len(entries)} curated CC0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
