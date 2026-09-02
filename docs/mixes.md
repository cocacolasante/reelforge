# AI Mix — one reel from every clip in a project

An AI mix meshes the best moments from **all** analyzed clips in a project
into a single reel. This is the cross-clip counterpart to per-asset
selection: selection ranks spans inside one video; a mix mines short moments
from every video, sequences them into one arc, and renders them as a
multi-source timeline.

## One-click pipeline

`POST /api/v1/projects/{id}/mixes` enqueues `create_mix_job`, which runs the
whole chain in one job:

```
mine (per clip) → pool (balanced) → contact sheets → sequence (1 Claude call)
     → validate → plan (style grammar → ReelTimeline) → persist → compose
```

1. **Mine** (`reelforge_core/mixes/mining.py`) — reuses the Selection v2
   candidate generators with short bounds: targets ≤90s mine 2–8s moments,
   longer targets mine 4–12s (keeps a 5-minute mix inside the editor's
   60-shot cap). Each clip's candidates get the standard prescore features +
   score, then near-identical spans (>0.85 time overlap) are dropped.
2. **Pool** (`pool_moments`) — balanced round-robin by prescore rank across
   assets (cap 60) so one clip can't monopolize the pool.
3. **Sequence** (`mixes/sequencer.py`) — ONE multimodal forced-tool call
   (`record_mix`): per moment a 3-frame contact sheet (shared with
   selection's cache) + a JSON block (bounds, features, transcript words,
   scene tags, energy). The model returns an ordered sequence with optional
   ±1s trims, plus title / hook / suggested_mood / content_style / reason.
   An optional **Direction** prompt (≤500 chars) steers both the picks and
   the style.
4. **Validate** (`validate_sequence`) — pure and strict: unknown ids
   dropped, trims clamped ±1.0s and speech-snapped, every shot ≥0.5s,
   total coerced into target ±20% (drop weakest / top up, payoff kept
   last), ≥3 shots or the deterministic `fallback_sequence` (top-prescore
   round-robin) takes over. A model failure can never fail the job.
5. **Plan** (`mixes/planner.py`) — a multi-source style grammar bakes
   pacing INTO the timeline: hype gets beat-cut pieces + a slow-mo on the
   global energy peak and alternating slides on source changes;
   talking_head gets jump cuts + punch-in alternation; cinematic/chill get
   their transition palettes. Output is a plain `ReelTimeline`
   (TimelineShots with speed / punch_in / transition_after) that satisfies
   every `PUT /reels/{id}/edit` validation rule — so the editor shows
   exactly what renders and stays hand-editable.
6. **Persist + compose** — the timeline, title, hook, mood, and style are
   written to the Reel row (`mixes/store.py`, sync SQLite), then the job
   composes inline via the standard timeline path with `style="classic"` +
   `director=False` (the sequencing call already did the content-aware
   pass). The mezzanine lands at `working/{primary}/reels/{mix_id}/` so
   preview, export, publish, and the editor all work unchanged.

## The synthetic Reel row

- id `mix-{uuid12}` — the **id prefix is the mix discriminator**.
  `child_reel_ids_json` stays NULL (that column discriminates montages).
- `asset_id` = the longest analyzed clip ("primary") — compose needs one
  real asset for analysis/probe/source, and its working dir hosts the
  render.
- `scene_indices_json = "[]"`, 4-key `scores_json`, no `mezzanine_path`
  pre-write (`GET /reels/{id}` syncs it from disk once composed).
- Title starts as "AI mix (working…)" and is replaced by the sequencer's
  title mid-job.

## API

- `POST /api/v1/projects/{id}/mixes` — body `{target_duration_sec: 15–300
  (default 45), prompt?: ≤500 chars, style?: auto|classic|hype|
  talking_head|cinematic|chill, aspect?, fps?}`. 409 `NOT_ENOUGH_CLIPS`
  unless ≥2 assets have an analysis.json (a 1-clip mix is just a reel).
  Returns the JobOut for the compose-kind job.
- `GET /api/v1/projects/{id}/mixes` — list with `edit_style`,
  `mezzanine_ready`, etc.
- Re-composing a mix uses the normal `POST /reels/{id}/compose`: the API
  passes a `reel_stub` built from the Reel row, and `compose_reel_job`
  falls back to it only when the id is absent from reels.json (the
  reels.json lookup always stays primary for real reels).
- `PATCH /reels/{id}/trim` and `DELETE /reels/{id}/edit` return 400 for
  `mix-` ids — a mix is defined by its timeline; edit it in the editor.

## UI

Reels page: the "AI mix" card (duration slider, Direction, style select)
creates a mix with one click and streams job progress; finished mixes list
above with a style chip and link to the standard reel detail page. On the
detail page mixes hide the max-length trim and "render AI cut instead"
controls; the editor hides "Reset to AI cut".

## Cost

One sequencing call per mix; with a full 60-moment pool expect roughly
25–35k input tokens (contact sheets dominate) + a few hundred output.
Recorded in `anthropic_usage` like selection.

## Deferred

- AI edit-director pass on mixes (needs a multi-analysis context refactor).
- Mixes in the project-level reel aggregation (they have their own card).
- Preference learning from user edits of mixed timelines.
