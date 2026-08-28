# Changelog

Format per [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
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
