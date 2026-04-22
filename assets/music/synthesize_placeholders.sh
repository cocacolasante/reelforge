#!/usr/bin/env bash
# Synthesize placeholder music tracks via ffmpeg's lavfi source. Deterministic
# and CC0. Runs during `docker build` so binary audio isn't in git.
set -euo pipefail

OUT_DIR="${1:-/app/assets/music}"
mkdir -p "$OUT_DIR"

FFLAGS="-hide_banner -loglevel error -y -f lavfi -t 90"
AFLAGS="-c:a libmp3lame -b:a 128k -ar 44100 -ac 2 -metadata creation_time=1970-01-01T00:00:00Z -fflags +bitexact -flags:a +bitexact"

# calm-01: gentle sine pad
ffmpeg $FFLAGS -i "sine=frequency=220:sample_rate=44100,aformat=channel_layouts=stereo,volume=0.3" $AFLAGS "$OUT_DIR/calm-01.mp3"

# tense-01: tremolo sawtooth
ffmpeg $FFLAGS -i "aevalsrc=0.25*sin(2*PI*110*t)*(0.5+0.5*sin(2*PI*6*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/tense-01.mp3"

# joyful-01: bright major-third pad (C4 + E4)
ffmpeg $FFLAGS -i "aevalsrc=0.20*(sin(2*PI*261.63*t)+sin(2*PI*329.63*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/joyful-01.mp3"

# somber-01: minor-third sub bass (A2 + C3)
ffmpeg $FFLAGS -i "aevalsrc=0.22*(sin(2*PI*110*t)+0.5*sin(2*PI*130.81*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/somber-01.mp3"

# energetic-01: square-wave arp (driven by a trigger at 4 Hz pattern)
ffmpeg $FFLAGS -i "aevalsrc=0.20*sgn(sin(2*PI*(220+40*(1-abs(1-mod(4*t\,2))))*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/energetic-01.mp3"

# mysterious-01: detuned chorus
ffmpeg $FFLAGS -i "aevalsrc=0.18*(sin(2*PI*196*t)+sin(2*PI*197.3*t)+sin(2*PI*293.66*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/mysterious-01.mp3"

# romantic-01: warm sine with vibrato
ffmpeg $FFLAGS -i "aevalsrc=0.22*sin(2*PI*(261.63+4*sin(2*PI*5*t))*t):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/romantic-01.mp3"

# triumphant-01: ascending C major triad held (C4 + E4 + G4)
ffmpeg $FFLAGS -i "aevalsrc=0.18*(sin(2*PI*261.63*t)+sin(2*PI*329.63*t)+sin(2*PI*392.00*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/triumphant-01.mp3"

# melancholic-01: slow A minor triad (A3 + C4 + E4)
ffmpeg $FFLAGS -i "aevalsrc=0.18*(sin(2*PI*220*t)+sin(2*PI*261.63*t)+sin(2*PI*329.63*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/melancholic-01.mp3"

# neutral-01: bandpass-filtered pink-ish noise (aevalsrc can't do noise; use sine mash)
ffmpeg $FFLAGS -i "aevalsrc=0.18*(sin(2*PI*200*t)+0.7*sin(2*PI*277*t)+0.5*sin(2*PI*330*t)):s=44100,aformat=channel_layouts=stereo" $AFLAGS "$OUT_DIR/neutral-01.mp3"

echo "synthesized placeholder tracks in $OUT_DIR"
