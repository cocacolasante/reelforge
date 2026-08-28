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

## Stage 2 — Score cheap (`reels/prescore.py`, no API)

Local features per candidate → a documented linear formula:

    +25  starts_on_unit_boundary      -40  starts_mid_word
    +15  ends_on_unit_boundary        -25  ends_mid_word
    +10 * min(energy_peak_z, 3)       +15  if energy_peak_pos < 0.2
    + 5 * min(n_scene_cuts, 4)        +10  if speech_ratio > 0.5

The shortlist walk keeps the top `shortlist_size` (40) in score order,
skipping anything overlapping an already-kept span by >0.85
(intersection / shorter). Everything lands in `prescore.json` for tuning.
`PRESCORE_VERSION` is part of the ranking resume stamp — bump it when the
weights change.

## Stage 3 — Rank rich (`reels/rank.py` + `reels/contact_sheet.py`, 1 call)

One multimodal listwise call: per candidate a 3-frame contact sheet
(opening / energy peak / closing, 180px-tall tiles ≈ 230 image tokens) plus
JSON context (word-timestamped transcript first/last 60 words, opening and
closing lines, per-second energy z, prescore features, scene summaries).
The model orders the whole set (`rank_position`, breaks score ties), scores
the four classic dimensions using the full 0-100 range, and describes what
is literally on screen in the first 2 seconds (`opening_description`).

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

The top-K get one `record_refinements` call: word timeline ±6s around each
edge, unit boundaries, energy. Every proposal is validated **locally**:
±6s window, duration within the config window (violations revert), mid-word
edges snap ≤0.6s or revert. `candidate_id` never changes; originals persist
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
