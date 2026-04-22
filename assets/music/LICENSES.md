# Bundled music licenses

All bundled tracks in this directory are synthesized at Docker build time from
simple waveforms via `ffmpeg -f lavfi`. They are not curated compositions — they
are deterministic placeholders that prove the music pipeline works end-to-end.
They are released into the public domain (CC0) as they contain no copyrightable
expression.

When you're ready to ship real music, replace these with curated CC0 tracks
(Pixabay Music, Free Music Archive CC0 pool) and update `manifest.json` to
reference the real files. Record each track's source and license here.

## Current tracks (synthesized placeholders, CC0)

| id              | mood         | bpm | notes                                   |
|-----------------|--------------|-----|-----------------------------------------|
| calm-01         | calm         | 70  | soft sine pad at 220 Hz                 |
| tense-01        | tense        | 120 | tremolo-modulated sawtooth              |
| joyful-01       | joyful       | 115 | major-third sine interval               |
| somber-01       | somber       | 60  | minor-third sub-bass sine               |
| energetic-01    | energetic    | 128 | fast square-wave arpeggio               |
| mysterious-01   | mysterious   | 85  | detuned chorus                          |
| romantic-01     | romantic     | 78  | warm sine with gentle vibrato           |
| triumphant-01   | triumphant   | 100 | ascending major triad                   |
| melancholic-01  | melancholic  | 64  | slow minor triad                        |
| neutral-01      | neutral      | 90  | white-noise bed with band-pass filter   |

Synthesis is performed by `assets/music/synthesize_placeholders.sh` which runs
in the Docker build of the worker/api/cli image.
