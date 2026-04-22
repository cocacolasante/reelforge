# Changelog

Format per [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
