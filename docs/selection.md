# Selection v2 — how reels get picked

Selection turns an analyzed asset (`analysis.json`) into a ranked list of
reel-able spans (`reels.json`). The v2 architecture is **generate wide →
score cheap → rank rich → refine edges → dedup on time**, five stages with
exactly two API calls (one ranking, one optional refinement).

## Stage 1 — Generate wide (`reels/candidates.py`, `reels/generators/`)

Three pure generators propose time-bounded spans; `generate_candidates`
unions them (exact `(start_ms, end_ms)` collisions dedup first-generator-wins)
and caps the union at `max_candidates` (400; sentence kept first, then scene,
then moment, even time-stride within a truncated group).

- **sentence** — builds *utterance units* from the word timeline (split on
  sentence punctuation, ≥0.45s gaps, or segment boundaries with ≥0.25s gaps;
  sub-second fragments merge forward) and enumerates unit-start→unit-end
  spans. Spans with <15% spoken time are skipped.
- **scene** — contiguous PySceneDetect scene runs inside the duration window
  (the classic enumerator).
- **moment** — peaks in the per-second energy track (`analysis/energy.py`:
  0.6·motion-z + 0.4·loudness-delta-z, local maxima ≥8s apart, top 25). Each
  peak spawns windows placing it at 15/35/55% of three durations, edges
  snapped to scene cuts or quiet audio within ±2s.

A candidate's identity is its time span: `candidate_id =
sha1(asset_id|start_ms|end_ms)[:16]`. `scene_indices` lists the scenes that
*cover* the span (for compose's per-scene clip extraction); compose clamps
the outer clip bounds to the reel bounds.

## Stage 1b — Keep cuts off the action (`reels/events.py`, no API)

Speech- and scene-aligned edges don't know what's on screen. In action footage
people talk *before* the action ("here it comes") and react *after* it, so
those edges land right before a wave hits or right after the fall.

- **Events.** `detect_events` builds a per-second activity track — the larger
  of the robust motion z-score and loudness prominence (dB over a ±15s rolling
  median, halved in spoken bins so loud voices don't count) — and turns runs
  that reach 3.0 (extended while ≥ 2.0, merged across 1s gaps) into event
  spans. Tuned on hand-labeled GoPro surf footage: 10 of 11 events found, no
  false positives. Each start is then walked back through the visible onset
  (activity ≥ 1.0, up to 3s) — detection alone trailed a rising wave by 4s.
  Written to `events.json` for debugging.
- **Guard.** `guard_span` forbids an END inside an event or within 4s before
  one (it needs 1.5s of follow-through), and a START inside an event, within
  3s before one (no lead-in), or within 1.5s after one (opens on the
  aftermath). It moves edges to the cheapest legal positions within 8s and the
  duration window, preferring to include the event over dropping it, never
  mid-word (both edges of a word are tried). Bounds compare with a 10ms
  tolerance, so a reel ending at the rounded file duration is the file end.
  Spans it can't fix pass through, take a −35 prescore penalty, and are
  removed by the final gate (`dedup.enforce_clean_edges`) whenever a clean
  reserve reel can take their slot.
- **Where it applies.** Every generated candidate (`generate_candidates`;
  moved candidates get a new time-span id), boundary-refinement proposals (a
  proposal whose fix leaves the ±6s window is rejected), and AI-mix trims (a
  trim that re-cuts an event reverts to the mined bound).
  `SelectionConfig.event_guard=False` disables it.
- **Needs real loudness + energy.** Clips analyzed before 2026-09 have a flat
  loudness track (and possibly no energy track) — re-run analyze with resume.

## Stage 2 — Score cheap (`reels/prescore.py`, no API)

Local features per candidate → a documented linear formula (p2):

    +25  starts_on_unit_boundary*     -40  starts_mid_word
    +15  ends_on_unit_boundary*       -25  ends_mid_word
    +10 * min(energy_peak_z, 3)       +15  if energy_peak_pos < 0.2
    + 5 * min(n_scene_cuts, 4)        +10  if speech_ratio > 0.5
    -35  edge_cuts_event              +10 * min(events_inside, 2)

    * only when speech_ratio >= 0.4 — on action footage people talk before
      and after the action, so speech-aligned edges are exactly the bad cuts.

The shortlist walk keeps the top `shortlist_size` (40) in score order,
skipping anything overlapping an already-kept span by >0.85
(intersection / shorter). Everything lands in `prescore.json` for tuning.
`PRESCORE_VERSION` is part of the ranking resume stamp — bump it when the
weights change.

## Stage 3 — Rank rich (`reels/rank.py` + `reels/contact_sheet.py`, 1 call)

One multimodal listwise call: per candidate a 5-frame contact sheet (2s
before the start / opening / energy peak / closing / 2s after the end — the
two outer frames red-bordered, black past the footage edge; 180px-tall tiles
≈ 380 image tokens) plus JSON context (word-timestamped transcript first/last
60 words, the words within 5s outside each edge, opening and closing lines,
per-second energy z, action events within 8s with their position — inside /
before_start / after_end / crosses_start / crosses_end — prescore features,
scene summaries). The prompt (v4) spells out that on action footage speech
announces the action before it happens and reacts after it, so a clip that
ends on "another one coming" is missing its payoff. The model orders the
whole set (`rank_position`, breaks score ties), scores the four classic
dimensions using the full 0-100 range, and describes what is literally on
screen in the first 2 seconds (`opening_description`).

The optional user **Direction** prompt (`SelectionConfig.prompt`) adds a
required `prompt_relevance` (0-100) field; candidates under 35 are dropped
before dedup (strict filter — zero matches → honest empty result).

## Stage 4 — Dedup + diversity (`reels/dedup.py`, no API)

Time-based overlap (intersection / shorter duration, strict `<` 0.5
threshold, nested spans read 1.0) drops near-duplicates greedily by score.
Then an MMR re-rank (`overall − λ·(scene-tag Jaccard + 0.25 same-mood)`,
`diversity_lambda` 8.0, halved under a Direction prompt, 0 disables) pushes
same-topic repeats down before the top-K cut.

## Stage 5 — Refine edges (`reels/refine.py`, 1 small call, best-effort)

The top-K get one multimodal `record_refinements` call (r2): per reel an
8-frame edge strip (3s and 1.5s before the start, the start, 1.5s in | 1.5s
before the end, the end, 1.5s and 3s after — outside frames red-bordered),
the word timeline ±6s around each edge, unit boundaries, energy, and nearby
action events. The prompt puts action first: a line announcing action is
lead-in, a reaction is follow-through. Every proposal is validated
**locally**: ±6s window, duration within the config window (violations
revert), mid-word edges snap ≤0.6s or revert, and the edge guard runs on the
result (a fix that would leave the ±6s window rejects the proposal). `candidate_id` never changes; originals persist
in `pre_refine_start/end_sec`. Refined edges that newly collide are dropped
and backfilled from the post-MMR reserve. Failures keep unrefined bounds —
selection always completes.

## Cost, resume, determinism

- Typical select: one ranking call (~8-12k input tokens for a silent asset,
  more with heavy speech; estimates in `pricing.py` are live-calibrated) +
  one refinement call (~3-5k). `--resume` replays both from
  `ranking_raw.json` / `refine_raw.json` at **zero tokens** when the stamps
  match.
- Temperature 0 rides `extra_body` (the 1.x SDK removed the kwarg); still
  not guaranteed byte-identical — `--resume` is the determinism path.

## Evaluating quality

Hand-label the spans you'd personally pick in
`tests/reels/eval/labels/<asset_id>.json` (format in that directory's
README), then:

    ./reelforge eval-select

A pick is *recalled* at K when a top-K reel covers ≥50% of it. The table
also shows candidate counts, elapsed, and token spend per asset. Tune the
prescore weights / λ against this, not vibes.
