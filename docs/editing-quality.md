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
| hype | beat-placed ~2.6s pieces, hard cuts, quick slides between scenes | slow-mo 0.5x + drifting punch-in on THE energy peak; 1.5x through lulls | (config) | energetic |
| talking_head | jump cuts through dead air, all hard cuts | punch-in 1.25 alternation | karaoke, centered | — |
| cinematic | dissolve/fadeblack alternating 0.8s | Ken Burns on every shot | static, lower third | — |
| chill | fade 0.6s | none | static, lower third | calm |
| classic | today's pre-v1 behavior (identity plan) | low-energy Ken Burns | (config) | — |

Style resolution: explicit `ComposeConfig.style` → the ranker's per-reel
`content_style` classification (`RankedReel.edit_style`, selection prompt
v3) → heuristics (talky→talking_head, energy peak→hype, else cinematic).
Non-classic grammars only engage in the smart-auto flow (`smart_mode` +
`transition.kind == "auto"`) or when named explicitly — manual configs stay
classic bit-for-bit. Direction-prompt wording ("fast cuts", "vlog",
"cinematic") steers the ranker's classification.

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
