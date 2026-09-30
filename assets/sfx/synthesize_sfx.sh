#!/usr/bin/env bash
# Synthesize the bundled sound effects (compose/sfx.py) at image build time.
# Deterministic (seeded noise), 48 kHz stereo PCM, peaks around -4 dBFS —
# the mix sets the level. No binary blobs in git and no licence to track;
# a real file dropped into /data/sfx/<kind>.wav replaces one of these.
#
#   pop    — a short pitch-dropping blip, for a key word
#   hit    — a low thump with a noise transient, for an impact
#   whoosh — band-limited noise swelling into a cut and falling away
#            (peak at 0.35s: compose/sfx.py PEAK_SEC)
set -euo pipefail
OUT_DIR="${1:-/app/assets/sfx}"
mkdir -p "$OUT_DIR"
FF=(ffmpeg -hide_banner -loglevel error -y)
FMT=(-ar 48000 -ac 2 -c:a pcm_s16le -fflags +bitexact -flags:a +bitexact)

"${FF[@]}" -f lavfi \
  -i "aevalsrc=0.95*sin(2*PI*(950*t-2600*t*t))*exp(-t*38):s=48000:d=0.14" \
  -af "afade=t=in:d=0.003,afade=t=out:st=0.11:d=0.03" \
  "${FMT[@]}" "$OUT_DIR/pop.wav"

"${FF[@]}" -f lavfi \
  -i "aevalsrc=0.75*sin(2*PI*(95*t-60*t*t))*exp(-t*7):s=48000:d=0.55" \
  -f lavfi -i "anoisesrc=color=pink:seed=7:amplitude=0.5:r=48000:d=0.55" \
  -filter_complex "[1:a]highpass=f=800,volume='exp(-t*55)':eval=frame[n];[0:a][n]amix=inputs=2:normalize=0,afade=t=out:st=0.45:d=0.1" \
  "${FMT[@]}" "$OUT_DIR/hit.wav"

"${FF[@]}" -f lavfi -i "anoisesrc=color=pink:seed=11:amplitude=0.8:r=48000:d=0.6" \
  -af "highpass=f=350,lowpass=f=4500,volume='3.6*if(lt(t,0.35),pow(t/0.35,2),pow(max(0,0.6-t)/0.25,2))':eval=frame" \
  "${FMT[@]}" "$OUT_DIR/whoosh.wav"

echo "sfx written to $OUT_DIR: $(ls "$OUT_DIR"/*.wav | xargs -n1 basename | tr '\n' ' ')"
