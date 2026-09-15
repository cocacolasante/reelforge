# Changelog

Format per [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- **One long video from several clips.** Choosing "Long single span" with
  two or more analyzed clips now builds a single long-form video (up to 30
  minutes): the AI picks the strongest whole sections from every clip, puts
  them in a sensible order (intro first, wrap-up last), and renders one
  editable video — instead of one long reel per clip. The AI mix builder on
  the reels page also goes up to 30 minutes. Very long timelines (up to 300
  shots) render in multiple chunk levels so memory stays bounded.
- **Upload several files at once.** Drop or pick any number of videos and
  photos; they upload one after another with the same resumable, parallel
  chunking as before. The panel shows "File 2 of 5", what's up next, and lets
  you add more, skip the current file, or cancel the rest. Files that can't be
  uploaded (unsupported type, over 5 GB, empty) are skipped with the reason
  instead of stopping the batch, and a summary lists what went up. The panel
  stays open for the whole batch.
- **Eye contact correction.** A per-render toggle (reel page and editor)
  nudges your eyes toward the camera when you glance at notes or a second
  screen. It moves only the inside of each eye — lids, lashes and skin stay
  put — pauses during blinks and head turns, and softens big glances rather
  than erasing them (larger shifts looked fake in testing). Looking down is
  only lightly corrected. Off by default; adds roughly 1.5–2 minutes of
  processing per minute of footage.
- **AI B-roll suggestions.** "Suggest B-roll" in the editor's B-roll card
  reads what you say (on the edited timeline, unsaved changes included) and
  proposes cutaways from the project's other footage and photos — each with
  the quote it illustrates and why. Accept, preview the spot, or skip each
  one; nothing is added until you accept. An optional direction steers it
  ("use the beach photos").
- **B-roll layers.** The editor has a B-roll track: "Clip at playhead" /
  "Photo at playhead" drop a project clip or photo over the talking head
  (3s by default) — full screen or picture-in-picture in any corner and
  size — while the voice keeps playing. Set start, length and where in the
  clip it plays from; the scrubbable preview shows layers in place. In the
  render, layers fade in/out, get the reel's color grade, and sit under
  captions and text.
- **Editor: cut dead air out of a shot.** Drag across a shot's waveform to
  select a section and click "Cut out" — the rest is rejoined with a jump
  cut. Clicking a waveform jumps the preview there (no more waiting for
  playback to reach a spot), the playhead is correct on sped-up/slowed
  shots, and the scissors split at the playhead instead of always in half.
  Voiceover waveforms are clickable too.
- **Delete projects from the UI.** A trash button on each project card and
  a "Delete project" button in the project header open a confirmation that
  lists what goes (clips, footage size, reels, renders, exports, mixes).
  `DELETE /projects/{id}` now matches clip deletion: running jobs are
  aborted first, and publications are removed with their reels — a project
  with a published reel previously failed to delete on the foreign key.
- **Action-aware cuts — live-run fixes.** A real selection on surf footage
  exposed four gaps, now closed: detected event starts trailed a rising wave
  by up to 4s (starts now walk back through the visible onset); a reel
  ending at the rounded file duration was treated as cutting the wipeout it
  contained (10ms bound tolerance); a mid-word pull-back could leave no
  legal end (both word edges are tried); and an unfixable event-cutting
  reel could still reach the top 5 — a final gate
  (`dedup.enforce_clean_edges`) now backfills its slot with a clean reel,
  and guard fixes in refinement may reach past the model's ±6s window.
  Refinement prompt r3: when an event won't fit, end before its build-up
  and announcing line, never in between.
- **Action-aware cuts — the models can see the action.** Contact sheets now
  include a red-bordered frame 2s before and 2s after each candidate, and the
  ranker sees the words just outside each edge plus nearby action events
  (prompt v4) — so "we got another one coming" at the end of a clip reads as
  a missing payoff. Boundary refinement gets an 8-frame edge strip, event
  context and action-first rules (r2). Prescore p2 only rewards
  speech-aligned edges on talky spans, penalizes edges the guard couldn't
  fix, and rewards whole events. The edit-director (d2) can no longer nudge
  a shot into an event.
- **Action-aware cuts — event guard.** Selection detects action events from
  motion + loudness (`reels/events.py`) and moves candidate edges, boundary
  refinements and AI-mix trims so no cut lands inside an event, right before
  one, or right after one — the reels that ended just before a wave hit or
  opened after the wipeout. Debug artifact `events.json`;
  `SelectionConfig.event_guard` toggles it.
- **AI Mix** — one reel meshing the best moments from EVERY clip in a
  project (`docs/mixes.md`). One click on the reels page: mines short
  moments per clip (Selection v2 generators + prescore), pools them
  balanced across sources, sequences them into a single arc with ONE
  multimodal Claude call (`record_mix`, locally validated with a
  deterministic fallback — a model failure can never fail the render),
  bakes an editing-style grammar into a multi-source `ReelTimeline`, and
  renders it inline. Target length 15s–5min, optional Direction prompt,
  style override. The result is a normal reel — preview, timeline editor,
  export, publish all work unchanged. New endpoints:
  `POST/GET /projects/{id}/mixes`; `compose_reel_job` gained a `reel_stub`
  fallback so mixes re-compose without a reels.json entry (real reels.json
  lookups stay primary); trim + edit-reset return 400 for `mix-` ids.
- **Edit Quality v1** — the renderer learned to actually edit
  (`docs/editing-quality.md`): per-shot speed (slow-mo/ramps) and punch-ins,
  eased direction-rotating Ken Burns, 15 transition kinds, per-cut
  transitions in AI-composed reels, beat-placed cuts, jump-cut silence
  removal, and hierarchical chunked rendering (fixes OOM on fast-cut edits).
  Four editing-style grammars (hype / talking head / cinematic / chill)
  plan each reel's cut deterministically; the selection ranker classifies
  each reel's `content_style` (prompt v3); an AI edit-director refines the
  plan within the grammar's bounds — stamped (free re-composes), locally
  validated, and able to add a hook title overlay. Compose panel gains a
  style dropdown + "AI edit direction" toggle with a server-fed plan
  preview; the timeline editor gains speed/punch-in controls, the full
  transition menu, and a 0.15s shot floor. Also fixes: smart mode dropping
  the reframe setting, the editor's dead per-shot Ken Burns toggle, and
  silently-broken xfades on short shots.
- **Selection v2** — full overhaul of reel selection (generate wide → score
  cheap → rank rich → refine edges → dedup on time; `docs/selection.md`):
  - Three candidate generators: sentence-aligned spans from the word timeline,
    the classic scene enumerator (count cap lifted 6 → 40), and
    moment-anchored windows around motion/loudness peaks from a new
    per-second energy track (`analysis/energy.py`, additive "energy" stage).
    Candidate identity is now the time span, not the scene list; compose
    clamps outer clip bounds to reel bounds.
  - Local heuristic prescore + shortlist (default 40) so the ranker only
    sees plausible candidates; the unsound >80 batched-ranking path is gone.
  - Multimodal listwise ranking: 3-frame contact sheets per candidate,
    word-timestamped context, explicit `rank_position`, literal
    `opening_description`, full 0-100 score range.
  - Best-effort boundary refinement of the top-K (±6s, locally validated,
    speech-safe; originals kept in `pre_refine_*`).
  - Time-based dedup + MMR diversity re-rank (`diversity_lambda`, halved
    under a Direction prompt) + post-refine overlap recheck with backfill.
  - Eval harness: hand-labeled picks in `tests/reels/eval/labels/` scored by
    `./reelforge eval-select` (recall@3/5/10 + cost per asset).
  - UI: source pill + "Opens on:" description on reel cards; Advanced
    disclosure (shortlist size / variety / refine toggle) in the selection
    panel. New `SelectionConfig` knobs accepted by the select endpoint:
    `max_candidates`, `shortlist_size`, `refine`, `diversity_lambda`.
  - Committed `uv.lock` (+ `uv sync --frozen` in the image) and pinned
    `anthropic>=1.0,<2`; ranking temperature rides `extra_body`.
- Curated music packs replacing the OpenGameArt placeholders: Scott Buckley
  (cinematic, CC-BY 4.0) + Loyalty Freak Music (lo-fi, CC0 via Internet
  Archive) via `scripts/fetch_music_packs.py`; CC-BY credit lines are
  auto-appended to descriptions/captions at publish time. New "Manage
  library" dialog (list / preview / upload / delete) with a Pixabay
  manual-pick guide.
- Natural-language selection prompt ("Direction"): describe the clips you want
  ("clips of falls", "jumps or carves", "make it feel intense") when selecting.
  The ranker scores each candidate's `prompt_relevance` (0-100); clips below
  the relevance floor are filtered out entirely (strict match — no matches
  yields an empty, clearly-messaged result), and the final order blends
  relevance (45%) with quality (55%). Style/feel wording steers the reel's
  suggested mood, which cascades into transitions, color grade, and music.
  Surfaced as a "Direction" field in the selection panel, a "Match %" badge on
  reel cards, and `--prompt` on `reelforge select`.

### Changed
- Blended `overall` under a prompt means dedup now prefers the more on-prompt
  span among overlapping candidates.
- `POST /assets/{id}/select` returns 422 `INVALID_CONFIG` for invalid bodies
  (e.g. prompt over 500 chars) instead of a 500.


### Fixed
- **Stray backslash in wrapped captions.** Every two-line static caption
  (the style AI mixes and long videos use) showed a literal "\" at the end
  of its first line: the line break was escaped a second time when the
  caption was written. Karaoke captions were unaffected.
- **Analyses stuck at 0% after a worker crash.** A worker process died
  mid-analysis (native segfault, exit 139) and, with no restart policy, stayed
  down; its jobs kept "running" until arq's in-progress locks expired an hour
  later. Workers now restart automatically and log a Python traceback for any
  native crash (`PYTHONFAULTHANDLER`).
- **"No clips matched your direction" when the length was the problem.**
  Asking for a single reel longer than the clip (e.g. 5 minutes from a
  2-minute video) generated zero candidates, and the project page blamed the
  direction prompt. Selection now shrinks a length the clip can't provide to
  what it has (the whole clip becomes the reel), and the direction note only
  appears when candidates existed and the AI rejected them all.
- **Reels page errored right after selection until "Try again".** The project
  page and the reels page list reels at the same moment when a selection
  finishes; both tried to register the same new reels and one request failed
  on a duplicate key. Registration is now conflict-safe.
- **Reel page crashed after loading ("Something went wrong").** A data hook
  ran after the page's loading check, so React saw a different hook count
  once the reel arrived (error #310). The compose panel is reachable again.
- **Auto-reframe ignored the subject since the OpenCV 5 upgrade.** OpenCV
  5's base wheel dropped the face detector, so subject tracking failed on
  every clip and quietly fell back to a centered crop. The image now ships
  opencv-contrib-python (needed by eye contact anyway) plus a bundled face
  cascade file, restoring motion- and face-following crops.
- **Talking-head silence removal clipped words and missed pauses.** Jump
  cuts trusted Whisper's word timestamps, which end up to 0.4s before the
  sound does; beat sync then trimmed up to 0.45s more off clip ends and the
  AI director nudged cut points by up to 1.5s. Dead air is now measured
  from the audio itself (pauses ≥0.45s, including ones straddling a scene
  split and at the reel's edges), beat-sync trims only eat trailing
  silence, and the director no longer moves talking-head cuts.
- **Loudness analysis was silently broken — every bin read -80 (silence)
  for every clip.** ebur128 ran with `framelog=verbose`, whose per-frame
  lines sit below ffmpeg's default log level, so the parser saw nothing.
  Everything built on loudness was blind: the energy track's audio half
  (wave crashes, splashes, shouts), moment edge-snapping, and loudness-dip
  scene splitting. Now `framelog=info`; zero parseable lines fail analysis
  loudly instead of writing a flat track; the loudness + energy resume
  stamps carry `LOUDNESS_VERSION` so `analyze --resume` recomputes them
  while scenes, transcripts and semantics stay cached. Re-analyze existing
  clips to pick it up.
- **`analyze --resume` silently stripped speech from analysis.json.**
  `transcribe()` writes a bare `Transcript` dump when there is speech, but
  the resume reader only understood the `{"transcript": ...}` wrapper, so
  every resumed run loaded `transcript=None` — and re-ran semantics with
  empty transcript slices. Both shapes now load via
  `pipeline._load_transcript_json`; affected clips are repaired by another
  resume run (the original semantics cache rows still match).

## [0.7.0] — 2026-04-22

### Added

- **Cost controls (§2).** Per-model pricing table at
  `packages/core/reelforge_core/pricing.py` + `anthropic_usage` SQLite
  table. New endpoints: `POST /assets/{id}/analyze/estimate`,
  `POST /assets/{id}/select/estimate`, `GET /projects/{id}/usage`,
  `GET /usage`. Workers record one row per completed LLM-using job.
- **Clip + music caches (§3).** Content-addressed `/data/cache/{kind}/`
  with LRU eviction. Cap via env (`CACHE_CLIPS_GB`, `CACHE_MUSIC_GB`,
  `CACHE_PREVIEWS_GB`). Same aspect/fps/duration → cache hit → re-compose
  typically 5-20× faster.
- **Compose presets (§3).** `compose_presets` table + CRUD endpoints.
- **Batch compose (§3).** `POST /assets/{id}/compose_batch` enqueues one
  job per `reel_id`.
- **Custom music upload (§4).** `POST /music/uploads`,
  `DELETE /music/{id}` (user tracks only — bundled tracks are immutable).
  Merged into the existing `load_music_library()` path.
- **Transcript override (§5).** `transcript_overrides` table + GET/PUT/DELETE
  endpoints. `build_captions` prefers the user-edited transcript when
  present; Whisper output acts as the fallback.
- **Reel trim offsets (§6).** `trim_start_offset_sec` +
  `trim_end_offset_sec` on the `Reel` row. `PATCH /reels/{id}/trim`
  validates ±2 s + 25 s minimum effective duration, and invalidates the
  existing mezzanine. Compose extracts the first and last clips at
  trim-adjusted timestamps; caption timeline mapping carries through.
- **Disk usage + cleanup (§10).** `GET /disk_usage`,
  `GET /projects/{id}/disk_usage`, `POST /projects/{id}/cleanup` with modes
  `safe | working | outputs | all`. `./reelforge cleanup --dry-run` CLI.
  `POST /cache/purge?kind=...` nukes a cache kind.
- **Log redaction (§11).** `AnthropicKeyRedactor` filter + tests. Installed
  at root logger on both API and worker boot. Scrubs the literal env key
  and pattern-matches `sk-ant-…` tokens defensively.
- **Production deploy (§8).** `compose.prod.yml` + `docker/nginx/` config
  + `build-and-publish.yml` GitHub Actions workflow (multi-arch GHCR).
- **CI (§7).** `.github/workflows/ci.yml` runs `pytest` inside the
  existing test-compose profile + builds the web image.
- **Docs (§12).** Full README rewrite, `docs/architecture.md`,
  `docs/deployment.md`, `docs/troubleshooting.md`, `docs/benchmarks.md`.

### Deferred (explicitly scoped out of this release)

- Full transcript-edit UI (backend plumbing ships; editor route is polish).
- Drag-to-trim UI handles on the scene timeline (backend ships).
- Single-user authentication flow (config surface and disabled-mode wiring
  done; login/logout routes + middleware land in 0.7.1).
- Sentry wire-up (env-var gate ships; SDK initialization is a follow-up).
- Full Playwright E2E in CI (YAML is wired; integration service for the
  mock Anthropic server is next).

### Changed

- `ComposeConfig` gains `trim_start_offset_sec` + `trim_end_offset_sec`
  (default 0).
- Worker job handlers now record `anthropic_usage` rows on success.

## [0.6.0] — Phase 6

Web UI (Next.js 14 + React Query + shadcn-style primitives + chunked
uploader + SSE progress + export download). See
`CLAUDE.md#phase-6-acceptance-status`.

## [0.5.0] — Phase 5

FastAPI + SQLModel + chunked resumable uploads + SSE streams + Range media
endpoints + interrupted-job recovery.

## [0.4.0] — Phase 4

Four export presets (MP4 H.264 social, MP4 H.265 HQ with `hvc1`, MOV ProRes
422 `apcn`, MOV ProRes HQ `apch`) + skip-if-exists by mezzanine hash +
output verification.

## [0.3.0] — Phase 3

Composition pipeline: clips → xfade → captions (ASS) → music ducking →
`mezzanine.mp4`. Byte-identical determinism with the same config + source.

## [0.2.0] — Phase 2

Reel selection: candidate enumeration + single batched Claude ranking call
+ greedy overlap-aware dedup → `reels.json`.

## [0.1.0] — Phases 0–1

Docker scaffolding + analysis engine (scenes, Whisper, ebur128, Claude
semantics) → `analysis.json`.
