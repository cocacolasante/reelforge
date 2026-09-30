# Edit Quality v1 — how reels get EDITED

Selection (docs/selection.md) picks *which* span becomes a reel; this layer
decides *how it's cut*. Architecture: **deterministic style grammar plans the
edit → AI edit-director refines the plan (one stamped call, locally
validated) → renderer executes per-shot/per-cut.**

## Renderer capabilities (compose/)

Per-shot (TimelineShot / styles.PlannedShot → ClipInfo):

- **speed** 0.25–4.0 — `setpts`/`atempo` at extraction. The `-ss/-to` seek
  window and reframe pan scale by 1/speed (output-side seeking acts on
  post-setpts timestamps). v1 rule: speed ≠ 1 renders the shot's own audio
  muted and suppresses its captions. Speed is part of both clip cache keys.
- **punch_in** 1.0–1.6 static digital zoom (graph-side — clip cache stays
  hot), `punch_in_animated` drifts the crop like Ken Burns.
- **Ken Burns** — eased (decelerating) drift, direction rotating with clip
  position (`graph_builder._drift_crop`); per-shot `ken_burns` works for
  timeline video shots; scene mode keeps the low-energy auto-trigger.

Per-cut: 15 transition kinds (fade/fadeblack/fadewhite/dissolve/slides ×4/
wipes ×2/smooths ×2/circles ×2/cut), per-cut choices survive photo
interleaving, every xfade is clamped to half its shorter neighbour
(`clamp_transitions` — applied before captions/beat-sync consume durations).
Beat sync runs for hard-cut reels too; `beats_in_range`/`BeatGrid.snap` place
cuts ON beats.

**Jump cuts** (`compose/jumpcuts.py`): silences ≥0.6s inside a shot are cut
out with 0.15s pads; no fragment under 0.4s. `ComposeConfig.jump_cuts`:
off | auto (style-driven) | on (forced).

**Hierarchical rendering**: >6 clips renders in chunks of ≤5 to
intermediates, then a final pass adds captions/music/LUT/loudnorm — a
12-clip 1080×1920 single-pass chain peaked ~6 GB and OOM'd; the mezzanine
timeline is identical either way.

## Style grammars (compose/styles.py)

| style | cuts | motion | captions | music bias |
|---|---|---|---|---|
| hype | beat-placed ~2.6s pieces, hard cuts everywhere | pieces of one shot alternate framing 1.0/1.2; slow-mo 0.5x + drifting punch-in on THE energy peak; 1.5x through lulls | (config) | energetic |
| talking_head | jump cuts through dead air, all hard cuts | framing keys: 1.0/1.15/1.0/1.3 cycle, a change ~every 3s at phrase boundaries (`rhythm_keys`) | punch, lower third | — |
| cinematic | dissolves 0.8s, one fadeblack before the last shot | Ken Burns on every shot | static, lower third | — |
| chill | fade 0.6s | none | static, lower third | calm |
| classic | today's pre-v1 behavior (identity plan) | low-energy Ken Burns | (config) | — |

Style resolution: explicit `ComposeConfig.style` → the ranker's per-reel
`content_style` classification (`RankedReel.edit_style`, selection prompt
v3) → heuristics (talky→talking_head, energy peak→hype, else cinematic).
Non-classic grammars only engage in the smart-auto flow (`smart_mode` +
`transition.kind == "auto"`) or when named explicitly — manual configs stay
classic bit-for-bit. Direction-prompt wording ("fast cuts", "vlog",
"cinematic") steers the ranker's classification.

**Framing keys (pro-editing CP3).** `[(t, zoom, cx, cy)]` per shot, t in
the shot's output seconds: the framing changes WITHIN a shot, so shot
durations and every piece of timing math stay put. The render graph scales
the frame up by the key's zoom and cuts a fixed output-sized window, both
retargeted by one `sendcmd` per key (resizing a crop mid-stream hangs
ffmpeg 5.1 when it grows). Keys win
over `punch_in`; the director can't punch a keyed shot.

**AI emphasis + SFX (pro-editing CP4).** One stamped Haiku call per reel
(`compose/emphasis.py`) picks caption key words, one key moment per line
(tight framing, maybe a pop), and misheard-word fixes (saved as the
transcript override). `compose/sfx.py` places whooshes on B-roll entries
and flashy transitions and pops on key moments, one per 6s at most, mixed
under the final bus.

## AI edit-director (compose/director.py)

One call per compose (`director_model`, default sonnet) that sees the plan +
per-shot energy/words/summaries + the style's constraint block and proposes:
per-cut transition choices from the palette, cut nudges ≤1.5s, speed/punch-in
placement, and an optional ≤40-char `hook_text` burned over the first 2s.

**Every proposal is validated locally** (`apply_director`, pure): palette
membership, per-style speed sets and minimum shot lengths, punch-in ≤1.5,
nudges clamped and speech-snapped — invalid entries revert individually.
Stamped by a fingerprint of (plan, style, model, prompt version) into the
reel dir (`director_raw.json` + `.stamp`): unchanged re-composes replay at
zero tokens; failures never stamp and keep the deterministic plan.
`ComposeConfig.director` (default on) / the "AI edit direction" checkbox
turns it off. Tokens: ~4.5k in / 0.4k out for a 22-shot plan
(pricing.py constants are live-calibrated).

## Config surface

`ComposeConfig`: `style` (auto|classic|hype|talking_head|cinematic|chill),
`director`, `director_model`, `jump_cuts`. CLI: `reelforge compose --style`.
Web: compose panel (smart section) has the style dropdown + director toggle;
`GET /reels/{id}/compose_plan` serves the preview (mood picks + style +
description) so the UI never duplicates server tables.

## Invariants for future work

- Anything that changes shot durations must thread the triplicated xfade
  math: `graph_builder._xfade_offsets`, captions' reclaim loop, and
  `compute_beat_end_trims`.
- Any new per-shot extraction parameter goes into BOTH clip cache keys.
- `STYLE_BOUNDS` (director validation) and the grammars must stay in sync —
  the director can only choose what the style's palette allows.
- The director must never be able to fail a render: validate-or-revert,
  never trust, never block.

## Measuring an edit (QA scorecard, 2026-09-30)

Every render now ends with `qa.json` next to `compose.json`
(`reelforge_core/qa/`): the finished edit measured against the targets in
`qa/thresholds.py`, per kind of reel (talking / action / long_form, chosen by
duration, speech ratio and style). It can never fail a render.

- **Visible changes are read from the edit plan, not from pixels.**
  Calibration on real renders showed ffmpeg's scene score can't separate a
  real cut from motion: a visible cut scored 0.042 while a near-identical
  junction scored 0.089 and a handheld pan 0.116. `compose.json` now records,
  per shot, the asset, duration, speed, punch-in and the transition into the
  next shot, so a junction is classified as `cut` (different clip), `jump`
  (source skips ≥ 0.5s), `reframe` (zoom step ≥ 0.1) or `invisible`. B-roll
  windows count on the way in and out. Manifests from before this change are
  scored but flagged `precise: false`.
- **Speech metrics** come from `words.json`, a sidecar `build_captions`
  writes whatever the caption mode: every spoken word in mezzanine time.
- **Captions** are measured from `captions.ass` itself (`qa/captions_geom.py`),
  so spoken captions, editor overlays and the director's hook are all checked
  against `compose/safezone.py` — the one definition of the platform-safe
  rectangle, which captions will share.

CLI: `reelforge qa <reel>`, `reelforge qa-project <project> --save-baseline
v0 | --diff v0`, `reelforge label <asset> 12.0-47.5 "note"`,
`reelforge seed-labels <project>` (exported reels become eval picks), and
`reelforge rate <reel> --verdict better|same|worse`.

## What is working now (research brief, 2026-09-30)

Evidence quality varies: platform docs and large studies are strong; most
"retention data" online comes from tool vendors and is directional at best.

**Short-form (TikTok / Reels / Shorts)**
- Hook: deliver the promise in the first 3 seconds (TikTok for Business);
  frame 1 visual, spoken hook immediately, no greeting; for action, cold-open
  on the payoff. YouTube Shorts reports "viewed vs swiped away".
- Pacing: talking head changes visual every ~1.5-2.5s but not faster than
  ~1.2s, tied to meaning (new idea, new visual). Standard auto kit: silence
  and filler jump cuts, alternating 110-130% punch-ins on emphasis, keyword
  B-roll, sparse sound effects. Academic evidence on jump-cut frequency is
  thin.
- Captions: 1-3 word chunks, heavy sans with stroke/shadow, keyword-only
  highlight, ~55-70% down the frame; heavy word-by-word animation is getting
  saturated ("highlighter, not confetti").
- Length: TikTok engagement peaks at 15-30s while median views peak at
  120-180s (6M brand videos, H1 2026); TikTok monetisation needs > 60s;
  Shorts and Reels go to 3 min.
- Audio: -14 LUFS / -1 dBTP is the safe target (platforms publish none);
  speech rate and loudness have an inverted-U effect on engagement (6,152
  Douyin videos); voice enhancement and music ducked ~15-20 dB are standard.
- Endings: end on the payoff with a hard cut, no outro card.
- Visual: smooth active-speaker tracking is table stakes; two speakers get a
  split or switch; normalise exposure and white balance; never burn in a
  watermark (Instagram demotes it as unoriginal).

**Long-form YouTube**: the first 30s confirm the title's promise within
5-10s (Studio reports the 0:30 "Intro" retention); visuals change every
~15-30s; a midpoint re-hook; chapters from 0:00 (≥3, ≥10s each); music beds
change at section boundaries.

**The bar set by AI editors** (Opus Clip, Submagic, Captions.ai, Descript,
CapCut, Vizard, Klap): transcript highlight finding, face-tracking reframe,
styled captions, silence and filler removal, auto punch-ins, auto B-roll,
speech enhancement and a clip score are table stakes. Differentiators
ReelForge can own: non-speech action moment detection, story-aware cold
opens, tasteful safe-zone-aware defaults, beat-synced action cutting, an
explainable score, and long-form retention editing.
