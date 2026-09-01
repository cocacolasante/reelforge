# ReelForge — working notes for Claude Code

Read this first whenever you resume work on this repo.

## What this is
Containerized pipeline that turns long-form video into 30–60s reels (MP4/MOV)
with music, captions, transitions, and effects. Every piece of the stack runs
in Docker; the only host requirement is Docker Engine ≥ 24 + Compose v2.

## Phase status
- [x] Phase 0 — Docker scaffolding & compose topology
- [x] Phase 1 — Ingestion & analysis engine (scenes + transcript + loudness + semantics → analysis.json)
- [x] Phase 2 — Reel selection (candidate generation + LLM ranking + dedup → reels.json)
- [x] Phase 3 — Composition (clips + transitions + captions + music + effects → mezzanine.mp4)
- [x] Phase 4 — Export presets (transcode mezzanine → 4 delivery presets)
- [x] Phase 5 — API & job orchestration (FastAPI + SQLModel + SSE)
- [x] Phase 6 — Web UI (Next.js 14 + React Query + Tailwind + shadcn)
- [x] Phase 7 — Polish: cost controls, caches, cleanup, docs, prod deploy scaffolding
- [x] Phase 7.1 — Multi-clip input + long-form output (montage + single span)
- [x] Phase 7.2 — Smart auto-direction: AI-driven transitions, LUTs, music, effects
- [x] Phase 8 — Quality pass: scene splitting for raw footage, CC0 music library,
      -14 LUFS loudness, karaoke captions, speech-safe cuts, beat sync,
      auto-reframe, encode-quality knob (2026-08-12/13)
- [x] Phase 9 — YouTube publishing: OAuth connect + resumable upload job + UI
      (`docs/publishing.md`; needs user's GOOGLE_CLIENT_ID/SECRET in .env).
      Verified live 2026-08-14 (multi-channel picker; first publish landed).
- [x] Phase 10 — Instagram + TikTok publishing. Instagram: Reels container
      flow — Meta FETCHES the video, so it needs the cloudflared tunnel
      (`--profile tunnel`) + REELFORGE_PUBLIC_MEDIA_BASE + tokened
      `/public/media/{token}` route. TikTok: FILE_UPLOAD to the user's
      TikTok INBOX (unaudited apps can't direct-post; user finishes in-app);
      access tokens rotate on refresh — always persist the returned
      refresh_token. Platform dispatch in `publish_reel_job`; per-platform
      OAuth in `routers/social.py`; UI platform tabs in PublishPanel.
- [x] Phase 11 — Edit Quality v1: renderer capabilities (speed/punch-in/
      15 transitions/jump cuts/chunked rendering), style grammars
      (hype/talking_head/cinematic/chill), ranker style classification
      (prompt v3), AI edit-director (stamped, locally validated).
      `docs/editing-quality.md`.
- [ ] Phase 12+ — not started

## Topology
```
web (Next.js, :3000) → api (FastAPI, :8001) → redis (arq broker) ← worker (arq)
                                              ↓
                                            /data volume (bind: ./data)
                                            /models/whisper (named: whisper_models)
```
- `api`, `worker`, `cli` all build from `docker/Dockerfile` (multi-stage targets).
- `web` builds from `docker/Dockerfile.web` (Next.js standalone output).

## Everyday commands
```bash
cp .env.example .env                       # first time only, then add ANTHROPIC_API_KEY
docker compose up -d                       # production-ish: api, worker, web, redis
docker compose -f compose.yml -f compose.dev.yml up   # hot-reload dev stack
docker compose --profile cli run --rm cli probe /data/inbox/foo.mp4   # or:
./reelforge probe /data/inbox/foo.mp4      # host shim, same effect

# Phase 1: analysis
./reelforge analyze /data/inbox/foo.mp4              # queued via worker (default)
./reelforge analyze /data/inbox/foo.mp4 --local      # in-process — debug path
./reelforge analyze /data/inbox/foo.mp4 --resume     # reuse intermediates from prior run
./reelforge analyze /data/inbox/foo.mp4 --model small --threshold 30

# Phase 2: reel selection (input = a completed analysis.json)
./reelforge select <asset_id>                        # queued
./reelforge select /data/inbox/foo.mp4               # accepts a source path too
./reelforge select <asset_id> --top 5 --overlap 0.3
./reelforge select <asset_id> --resume               # reuse ranking_raw.json if stamp matches
./reelforge select <asset_id> --local                # in-process — debug path
./reelforge select <asset_id> --json | jq '.reels[0]'

# Phase 3: composition (input = a completed reels.json)
./reelforge compose <asset_id> --reel 1              # render rank-1 reel, 9:16 with auto-mood music
./reelforge compose <asset_id> --reel <candidate_id> # by stable id instead of rank
./reelforge compose <asset_id> --reel 1 --aspect 16:9 --caption-mode karaoke
./reelforge compose <asset_id> --reel 1 --transition slideleft --no-music
./reelforge compose <asset_id> --reel 1 --music-track joyful-01 --lut identity
./reelforge compose <asset_id> --reel 1 --local      # in-process — debug path

# Phase 4: export presets (input = a completed mezzanine.mp4)
./reelforge export <asset_id> --reel 1 --preset mp4_h264_social
./reelforge export <asset_id> --reel 1 --preset mp4_h265_hq
./reelforge export <asset_id> --reel 1 --preset mov_prores_422
./reelforge export <asset_id> --reel 1 --preset mov_prores_hq
./reelforge export <asset_id> --reel 1 --all          # all four, sequentially
./reelforge export <asset_id> --reel 1 --preset mp4_h264_social --force   # re-transcode

# Tests (inside the test profile, same image as worker)
docker compose --profile test run --rm test            # default: pytest -ra -v
docker compose --profile test run --rm test pytest -k cache_key

docker compose run --rm cli bash           # drop into a shell for debugging
docker compose build --no-cache api worker # rebuild after dependency changes
docker compose down                        # stop; volumes (whisper_models, ./data) persist
curl -fsS http://localhost:8001/health     # API health
docker compose logs worker | jq            # structured JSON logs from the worker
```

## Seam locations
- **Ingestion / probe** — `packages/core/reelforge_core/ingest.py` (`probe()`, `MediaAsset.from_path()`, `ProbeResult`). `MediaAsset.has_audio` + `color_transfer` live here.
- **Shared paths** — `packages/core/reelforge_core/paths.py` (reads `REELFORGE_DATA_DIR`).
- **Atomic writes** — `packages/core/reelforge_core/io_utils.py` (`write_json_atomic` used by every pipeline stage).
- **Data models** — `packages/core/reelforge_core/models.py` (Pydantic: `Scene`, `Transcript*`, `LoudnessPoint`, `SceneSemantics`, `AnalysisConfig`, `AnalysisReport`, `ProgressEvent`, `STAGE_WEIGHTS`).
- **Error hierarchy** — `packages/core/reelforge_core/errors.py` (`AnalysisError` + one subclass per stage).
- **SQLite cache + job bookkeeping** — `packages/core/reelforge_core/db.py` (WAL mode; semantics_cache + jobs tables).
- **Analysis pipeline (Phase 1)**:
  - `analysis/scenes.py` — PySceneDetect + HDR bump + parallel thumbnail extraction (`build_scene_models` + `extract_thumbnails` helpers reused by the split step).
  - `analysis/segments.py` — long-take splitting: scenes > `max_scene_sec` (45s default) are split into ~`scene_split_target_sec` (40s) pieces at speech pauses → loudness dips → even grid. Runs in `pipeline.py` after loudness; rewrites `scenes.json` + re-extracts thumbs. Idempotent on resume. Without this, raw unedited footage (GoPro runs etc., few hard cuts) yields ZERO reel candidates because the enumerator only composes whole scenes.
  - `analysis/audio_extract.py` — one-shot mono 16 kHz PCM extraction shared by transcribe + loudness.
  - `analysis/transcribe.py` — faster-whisper with VAD + auto device/compute selection.
  - `analysis/audio.py` — ffmpeg `ebur128` → 1-second LUFS bins.
  - `analysis/energy.py` — per-second energy track (v2): motion = mean abs
    grey frame diff via SEQUENTIAL decode at `energy_sample_fps` (2.0), using
    the shared helpers in `reelforge_core/vision.py` (also used by
    compose/reframe.py — keep them in sync); `loudness_delta` between adjacent
    LUFS bins (clamped at the −80 silence sentinel). Bins centered i+0.5 like
    loudness. Writes `energy.json` + stamp `(source_mtime, sample_fps)`;
    `AnalysisReport.energy` defaults to [] so old analysis.json still loads
    (the moment generator just won't run). Stage "energy" (weight 0.03, carved
    from semantics) sits between loudness and semantics in STAGE_WEIGHTS —
    insertion order matters to `compute_overall`.
  - `analysis/semantics.py` — forced tool-use calls with retry + SQLite cache.
  - `analysis/pipeline.py` — `analyze()` orchestrator with weighted progress + resume stamps.
- **Selection pipeline (Phase 2)**:
  - `reels/candidates.py` — pure candidate generators unioned by
    `generate_candidates` (scene + `generators/sentence.py` +
    `generators/moment.py`). Union dedups exact (start_ms, end_ms) collisions first-generator-wins
    and caps at `SelectionConfig.max_candidates` (400; sentence kept first,
    then scene, then moment, even time-stride within a truncated generator).
    `generators/sentence.py` builds utterance units from the word timeline
    (punctuation / ≥0.45s gap / segment boundary + ≥0.25s gap; <1s units merge
    forward) and enumerates unit-start→unit-end spans, skipping spans with
    <15% spoken time. **v2 contract: `start_sec`/`end_sec` are the authoritative
    reel bounds and the identity** — `candidate_id =
    sha1(asset_id|start_ms|end_ms)[:16]`; `scene_indices` = scenes that COVER
    the span (`covering_scenes`, half-open `[start, end)`), for compose's
    per-scene clip extraction. `ReelCandidate.source` names the generator.
    Compose clamps outer clip bounds to reel bounds in `clips.py::clip_bounds`
    (`reel_start`/`reel_end` params) — mirrored in captions' fallback path,
    the beat-sync planned durations in `compose/pipeline.py`, and the editor's
    `_default_timeline` in `apps/api/routers/reels.py`. Scene-aligned
    candidates clamp to their own edges (no-op), so pre-v2 behavior is
    byte-identical for them.
  - `reels/evaluate.py` — eval harness: recall@K of `reels.json` against
    hand-labeled picks in `tests/reels/eval/labels/` (mounted into the cli
    service); `./reelforge eval-select`.
  - `reels/features.py` — re-exports `flatten_words` from
    `compose/speech_snap.py` (one definition of the word timeline; the
    dependency edge is one-way reels→compose).
  - `reels/prescore.py` — heuristic pre-score + shortlist (pure, no API):
    per-candidate features (unit-boundary/mid-word edges, speech_ratio,
    energy peak pos/z, lufs_range, scene cuts) → documented linear formula →
    `shortlist()` walk in score order skipping >0.85 time-overlap with kept.
    `SelectionConfig.shortlist_size` (40) caps what the ranker sees. All
    candidates + features + scores land in `prescore.json` (debug).
    `PRESCORE_VERSION` is part of the ranking resume stamp — bump it whenever
    the weights change.
  - `reels/rank.py` — **one multimodal listwise Anthropic call on the
    prescore shortlist** (SYSTEM_PROMPT_V2 / ranking_prompt_version "v2").
    Message = intro text block + per candidate a contact-sheet image block +
    a JSON text block (transcript_words first/last 60 with "…", opening/
    closing line, per-sec energy z, prescore_features, scenes[]). Required v2
    tool fields: `rank_position` (explicit order, breaks overall ties) +
    `opening_description` (literal first-2s). max_tokens 16000. Forced
    tool-use with one corrective retry for missing candidates. Temperature
    rides `extra_body` (SDK 1.x removed the kwarg; sonnet-4-5 still accepts).
    The old >80 overlapping-batch path is deleted (first-seen-wins merging
    across batches scored on different scales was never sound); oversize sets
    truncate to the first `LARGE_SET_THRESHOLD` (80) in prescore order.
  - `reels/contact_sheet.py` — pure sheet command builder: 3 frames
    (start+0.5 / energy peak / end−0.5) scaled to 180px HEIGHT (fixed height
    bounds image tokens ≈ w*h/750 across aspects; ~230/sheet), hstacked JPEG
    q≈75 → `working/{asset_id}/candidates/{candidate_id}.jpg`. Extraction
    (I/O, parallelism 4, skip-if-exists, best-effort — missing source just
    means text-only ranking) lives in `reels/pipeline.py`.
  - `reels/refine.py` — CP7 boundary refinement: ONE small API call
    (`record_refinements`, forced tool-use via rank's `_call_model` with
    `tool_name=`) proposing new bounds for the top-K; pure `apply_refinement`
    validates everything locally (±6s window, duration within effective
    min/max — violations revert, mid-word edges snap ≤0.6s or revert,
    `covering_scenes` recomputed). `candidate_id` NEVER changes; originals
    kept in `pre_refine_start/end_sec`. Best-effort: failures keep unrefined
    bounds. `refine_raw.json` + stamp `(model, refine_prompt_version r1,
    top-K ids+bounds)`; `SelectionConfig.refine` (default on). Live-verified:
    the model does propose floor-violating durations — the local validator is
    load-bearing, keep it strict.
  - `reels/dedup.py` — TIME-based greedy dedup (intersection / shorter
    duration, strict `<` threshold), `mmr_diversify` (score = overall −
    λ·max_sim; sim = scene-tag Jaccard + 0.25 same-mood bonus;
    `SelectionConfig.diversity_lambda` 8.0, halved under a user prompt, 0
    disables), `resolve_post_refine_overlaps` (refined edges can newly
    collide — drop the lower-ordered, backfill from the post-MMR reserve),
    and `assign_ranks_and_truncate`. Final pipeline order: rank → relevance
    gate → dedup → MMR → truncate top-K → refine → overlap recheck → ranks.
    NOTE: reel `rank` follows the MMR order and may deviate from pure
    overall-desc; the project-level aggregation still re-sorts by
    overall_score. `candidates_dropped_by_diversity` on ReelSelection counts
    top-K displacement.
  - `reels/pipeline.py` — `select_reels()` orchestrator with stage-weighted progress (candidates 5% / ranking 90% / dedup 5%) + `ranking_raw.json.stamp` resume.
- **Composition pipeline (Phase 3)** — `packages/core/reelforge_core/compose/`:
  - `graph.py` — `FilterNode`/`FilterGraph` DSL, `_ffescape` for filter-argument escaping, `run_ffmpeg` subprocess wrapper that raises `FFmpegError` with stderr tail, and `ffmpeg_version()`.
  - `clips.py` — `build_clip_command` + async `extract_clips` (accurate-seek re-encode; scale+pad with letterbox; HDR tonemap when `color_transfer in {smpte2084, arib-std-b67}`; bit-exact + zeroed creation_time for determinism).
  - `captions.py` — `build_captions` writes ASS; `_source_to_mezz_time` maps source timestamps to the mezzanine timeline (xfade overlap subtracted). Static mode packs into lines; karaoke mode emits one Dialogue per word.
  - `music.py` — `load_music_library` (bundled + user manifests), seeded `select_track` (mood → fallback to neutral → None), `build_music_prep_command`/`prepare_music` (stream-loop + trim + in/out fade).
  - `graph_builder.py` — `build_final_command` assembles clips + xfade/acrossfade chain + Ken-Burns on low-energy scenes + LUT + unsharp + subtitles + music sidechain/duck/mix into a single FFmpeg argv. **Splits `[voice]` via `asplit` so it can be both the sidechain key and the amix primary.**
  - `pipeline.py` — `compose()` orchestrator; stage weights `prepare 2% / clips 35% / captions 3% / music 5% / render 53% / finalize 2%`; parses `time=HH:MM:SS.ms` from FFmpeg stderr for live render progress. **First step is `resolve_smart_config()`** so `compose.json` reflects the resolved picks.
  - `auto.py` — smart-mode pickers: `TRANSITION_BY_MOOD`, `LUT_BY_MOOD`, `pick_transition_kind`, `pick_lut_id`, `pick_transition_kind_for_montage`, `resolve_smart_config` (pure — returns new `ComposeConfig`), `describe_smart_picks`. See Phase 7.2 acceptance below.
- **Export pipeline (Phase 4)** — `packages/core/reelforge_core/export/`:
  - `presets.py` — four `PresetSpec` records + `PRESET_SPEC_VERSION` + `PRORES_FOURCC` lookup. ProRes 422 uses `profile:v=2` (fourcc `apcn`); HQ uses `profile:v=3` (fourcc `apch`). Both set `vendor=apl0` so Apple apps recognize the files.
  - `command.py` — `build_export_command` — pure, no filters. Explicit `-map 0:v:0 -map 0:a:0`; bit-exact-friendly zeroed `creation_time` metadata.
  - `verify.py` — `verify_export` runs ffprobe against the output, checks codec/pixfmt/fourcc/audio-present/duration-drift. `sanity_check_size` warns (not fails) when size diverges from the preset's typical ratio.
  - `pipeline.py` — `export()`. Skip-if-exists keyed on `(input_mezzanine_sha256, preset_spec_version)` in the sidecar. Broken outputs are kept on disk for forensics (not deleted). Stage weights `prepare 5% / transcode 90% / finalize 5%`.
- **Publishing (Phase 9)** — `packages/core/reelforge_core/publish/`:
  - `youtube.py` — OAuth token flows + resumable upload via plain httpx REST
    (no google client lib). `PublishError` carries human-readable messages.
  - `store.py` — sync SQLite reads/updates of `social_accounts` +
    `publications` for the worker (jobstate.py pattern).
  - API: `apps/api/routers/social.py` (connect/callback/disconnect, publish
    enqueue with EXPORT_NOT_READY/SOCIAL_NOT_CONNECTED/SOCIAL_NOT_CONFIGURED
    409s, publications list). Worker: `publish_reel_job`. Job kind "publish"
    is in JobKindLit + the web Zod enum. UI: PublishPanel on the reel page.
- **Shared worker progress** — `apps/worker/progress.py::make_throttled_progress_writer` (500 ms throttle; terminal events always emit). Used by `analyze_asset`, `select_reels_job`, `compose_reel_job`, and `export_reel_job`.
- **Worker encoder check** — `apps/worker/main.py::_verify_ffmpeg_encoders` runs on boot and fails loudly if libx264 / libx265 / prores_ks / aac / pcm_s16le are missing from the image.
- **API service (Phase 5)** — `apps/api/`:
  - `main.py` — FastAPI app factory + lifespan (init DB, open arq pool, reset any `queued/running` jobs to `failed` with message "interrupted by restart", start background upload-purge loop). Every unhandled error returns the `ErrorEnvelope` shape.
  - `settings.py` — Pydantic BaseSettings; `data_dir` from `REELFORGE_DATA_DIR`, `redis_url` from `REDIS_URL`, all other values have defaults.
  - `db.py` — SQLModel entities (`Project`, `Asset`, `Job`, `Reel`, `Export`, `UploadSession`, `SemanticsCache`), async engine on `NullPool`, WAL + `busy_timeout=5000` pragmas, `foreign_keys=ON`. Includes an in-place migration that drops the legacy Phase-1 `jobs` table if its old `job_id` PK is on disk (no prod jobs existed pre-Phase-5).
  - `middleware.py` — request-id stamp + structured JSON access log (keys `ts,level,logger,request_id,method,path,status,duration_ms`).
  - `schemas/` — Pydantic response schemas. **DB entities never returned directly** from endpoints.
  - `routers/` — `health`, `projects`, `uploads`, `pipeline` (analyze+select), `compose`, `reels`, `exports`, `jobs`, `media`, `music`.
  - `services/jobs.py` — `enqueue_job` creates the DB row first, then calls `arq_pool.enqueue_job(..., _job_id=row.id)` so arq's job id equals our DB id. Conflict detection: supply a SQLAlchemy clause, returns 409 `JOB_ALREADY_RUNNING` if any matching `queued|running` row exists.
  - `streaming.py` — HTTP `Range: bytes=...` helper (200 for full body, 206 for partial, 416 for unsatisfiable). Supports `bytes=start-end`, `bytes=start-`, and `bytes=-suffix`. Used for reel preview, export download, and music audio.
- **Shared job state** — `packages/core/reelforge_core/jobstate.py` — sync SQLite writes wrapped by `asyncio.to_thread`. Workers call `mark_job_running`, `record_job_success`, `record_job_failure`, `append_log`. These target the same `jobs` table the API reads. Worker code that still does `from reelforge_core import db; db.record_job_*` continues to work via shims in `db.py`.
- **Web UI (Phase 6)** — `apps/web/`:
  - **Stack** — Next.js 14 App Router + TypeScript strict + Tailwind + shadcn-style primitives (not the CLI; hand-rolled in `components/ui/` from Radix primitives) + Inter via `next/font` + React Query v5 + Zustand for the uploader's chunk state. `output: "standalone"` for a ~150 MB runtime image.
  - **Pages** — `app/page.tsx` (project list + create modal), `app/projects/[projectId]/page.tsx` (three-zone: uploader → analyze → select), `app/projects/[projectId]/reels/page.tsx` (ranked list with 3-thumb preview strip), `app/projects/[projectId]/reels/[reelId]/page.tsx` (preview + compose config + export grid).
  - **API client** — `lib/api/client.ts` is the single `fetch` wrapper. Every hook in `lib/api/hooks.ts` validates its response with a Zod schema from `lib/api/schemas.ts`; validation failure raises `APIError("INVALID_RESPONSE")` so API/UI drift is caught immediately. Error envelopes from the server are parsed into `APIError(code, status, message, details)`; user-facing copy comes from `ERROR_MESSAGES` in `lib/api/errors.ts`.
  - **Uploader** — `lib/upload/uploader.ts` is a per-project Zustand store + hook. Protocol: POST `/uploads` → streaming PUT per chunk with parallelism 3 (each `file.slice(s, e).arrayBuffer()` so memory doesn't balloon on multi-GB files) → POST `/complete`. Persists the in-flight session id to `localStorage` so a page reload can resume. `AbortController` per chunk for pause; exponential-backoff retries up to 3×.
  - **SSE** — `lib/sse/job-stream.ts::useJobStream(jobId)` opens `EventSource(API/jobs/{id}/stream)`; on `onerror` falls back to 1.5 s polling of `GET /jobs/{id}`. Stops on terminal event.
  - **JobProgress** — one component covers all four job kinds; analyze + compose render stage steppers (probe → scenes → transcribe → loudness → semantics / prepare → clips → captions → music → render → finalize); select + export are single progress bars.
- **API entrypoint** — `apps/api/main.py` (`app` = FastAPI instance; `/health`).
- **Worker entrypoint** — `apps/worker/main.py` (`WorkerSettings` with `analyze_asset`).
- **Worker jobs** — `apps/worker/jobs.py` (`analyze_asset`, `select_reels_job`, `compose_reel_job`, `export_reel_job`; all stream progress to Redis `job:{id}:progress`). `WorkerSettings.max_jobs=2` because compose/export saturate CPU; scale horizontally with `docker compose up --scale worker=N`.
- **Worker logging** — `apps/worker/logging_config.py` (structured JSON; `docker compose logs worker | jq`).
- **CLI entrypoint** — `apps/cli/main.py` (Typer: `version`, `probe`, `analyze`, `select`, `compose`, `export`).
- **Web app** — `apps/web/app/` (App Router; `NEXT_PUBLIC_API_URL` env).
- **In-container `reelforge` script** — `docker/entrypoints/reelforge` → `python -m apps.cli.main`.
- **Host `reelforge` shim** — `./reelforge` → `docker compose --profile cli run --rm cli ...`.

## Layout conventions
- **No uv workspace.** Single root `pyproject.toml`. Code is reached via
  `PYTHONPATH=/app:/app/packages/core` set in the Dockerfile. `apps/api/main.py`
  therefore imports as `apps.api.main`. (The original spec had per-app
  `pyproject.toml` files; I collapsed to a single project because the workspace
  setup conflicts with the `apps.api.main:app` module path the spec expects.)
- **Non-root containers.** Every image runs as `reelforge` (uid 1000).
  Volumes are chowned in the Dockerfile before `USER reelforge`.
- **Image pins.** Every `FROM` uses a concrete tag; no `:latest`.
- **`.dockerignore` excludes `data/`.** The bind mount at runtime injects it.

## Build caching
The Python image has four stages: `system` (apt + ffmpeg), `python-deps`
(`uv sync --no-install-project` — deps only), `core` (source copy + chown +
`USER reelforge`), then `api` / `worker` / `cli` targets. Dependency changes
only invalidate from `python-deps` onward; code changes only invalidate `core`
onward. `uv.lock` isn't committed yet — `uv sync` falls back to resolving from
`pyproject.toml` on first build, which writes a lockfile inside the image.

## Phase 1 artifacts on disk
Per-asset working dir: `/data/working/{asset_id}/`.
- `probe.json` — snapshot of `MediaAsset` (id, path, size, probe fields).
- `thumbs/scene_NNNN.jpg` — one per scene, 4-digit zero-padded.
- `scenes.json` + `scenes.json.stamp` — scene intervals + the config that produced them.
- `audio.wav` — mono 16 kHz PCM, kept so transcribe + loudness share the extraction.
- `transcript.json` (+ `.stamp`) — word-level faster-whisper output, or `{"transcript": null}` for silent sources.
- `loudness.json` (+ `.stamp`) — 1-second LUFS bins. `ebur128.stderr.log` is written then deleted.
- `semantics.json` — per-scene Claude tool-use result (cached in `/data/reelforge.db`).
- `analysis.json` — the merged report. **Downstream phases read only this file.**

Resume: each stage checks its `.stamp` matches `(config, source_mtime)` before skipping. Semantics use the SQLite cache keyed on `(asset_id, scene_index, model, prompt_version, thumb_sha256, transcript_slice_sha256)`.

## Phase 2 artifacts on disk (Selection v2 — see docs/selection.md)
Same working dir, per asset:
- `candidates.json` — the generator union (scene + sentence + moment),
  time-bounded spans with covering `scene_indices`. Written even when empty.
- `prescore.json` — EVERY candidate with its features, prescore, and a
  `shortlisted` flag, sorted by score. The place to look when tuning weights.
- `candidates/{candidate_id}.jpg` — 3-frame contact sheets (960×~180) for the
  shortlisted candidates. Skip-if-exists; safe to delete (regenerated).
- `ranking_raw.json` — the parsed tool-use `rankings` array straight from Claude; untouched by dedup. Invaluable for debugging.
- `ranking_raw.json.stamp` — `(ranking_model, ranking_prompt_version,
  temperature, candidate_hash [over the SHORTLIST], prescore_version,
  prompt [only when set])`. When `--resume` is set and the stamp matches,
  `ranking_raw.json` is re-parsed instead of calling Claude.
- `refine_raw.json` (+ `.stamp` keyed `(model, refine_prompt_version,
  top-K ids+pre-refine bounds)`) — the boundary-refinement proposals; only
  written on a successful call so failures retry next run.
- `reels.json` — the merged `ReelSelection`. **Phase 3 and Phase 5 read only this file.**

Determinism: Claude at `temperature=0` (passed via `extra_body`; the 1.x SDK
removed the kwarg) is usually — but not always — byte-identical across
back-to-back calls. The reliable determinism path is `--resume`: two
consecutive `--resume` runs are byte-identical (excluding
`created_at`/`elapsed_sec`) and cost zero tokens (ranking AND refinement both
stamp-cached). If exact reproducibility matters, snapshot `ranking_raw.json`.
Eval: hand-label picks in `tests/reels/eval/labels/` and run
`./reelforge eval-select` for recall@K against them.

## Phase 3 artifacts on disk
Per-reel dir: `/data/working/{asset_id}/reels/{reel_id}/` (where `reel_id` = `candidate_id` from Phase 2).

- `clips/clip_NNNN.mp4` — normalized trimmed clips (one per scene in the reel), re-encoded at `ultrafast` CRF 18 to target resolution / fps. Letter/pillarboxed. HDR source is tonemapped. Bit-exact + zeroed metadata for determinism.
- `captions.ass` — ASS subtitles on the mezzanine timeline (xfade overlap subtracted). Empty-but-valid file when mode=off or no transcript.
- `music.wav` — 48 kHz stereo PCM trim of the selected track with 0.5s in / 1.0s out fade. Absent when `--no-music` or no library match.
- `mezzanine.mp4` — the canonical render. H.264 / yuv420p / AAC / +faststart / bit-exact. **Phase 4 transcodes from this file.**
- `compose.json` — `ComposeManifest` recording exactly what was rendered (config, scene→clip map, chosen music, ffmpeg version, duration).
- `ffmpeg_commands.log` — every FFmpeg invocation with full args. Copy-paste any line to reproduce the step outside the container. Rotated to `.1` at 10 MB.
- `tmp/` — scratch. Purged on success; kept on failure for inspection.

**Mezzanine-as-source-of-truth contract for Phase 4:** downstream export presets read only `mezzanine_path` and the `ComposeConfig.aspect` / resolution from `compose.json`. They MUST NOT re-run composition; changing export format costs one transcode pass, not a re-render.

Determinism: `compose.mezzanine.mp4` is **byte-identical** across back-to-back runs with the same `ComposeConfig` and source (verified by `test_compose_deterministic_byte_identical`). libx264 is deterministic at fixed preset + crf; the pipeline sets `-fflags +bitexact`, `-flags:{v,a} +bitexact`, and zeroes `creation_time`. Music selection is seeded by `(candidate_id, config.seed)`.

## Phase 4 artifacts on disk
Per-reel output dir: `/data/outputs/{asset_id}/{reel_id}/`.

- `mp4_h264_social.mp4` — Instagram/TikTok/Shorts delivery. H.264 High @ 4.1, yuv420p, CRF 20, AAC 192k, `+faststart` so browsers can stream-play without downloading the whole file first.
- `mp4_h265_hq.mp4` — High-efficiency distribution. HEVC Main, yuv420p, CRF 22, AAC 256k, `tag:v hvc1` so Safari/QuickTime recognize it (the default `hev1` tag silently fails to play in Safari).
- `mov_prores_422.mov` — Editorial handoff at ProRes 422 Standard. `prores_ks -profile:v 2` → fourcc `apcn`. yuv422p10le. pcm_s16le audio. `vendor=apl0`.
- `mov_prores_hq.mov` — Editorial handoff at ProRes 422 HQ. Profile 3 → fourcc `apch`. Same pixel format + audio as 422 Standard; higher bitrate budget internally.
- `{preset_id}.export.json` — sidecar manifest per output. Records `preset_spec_version`, `input_mezzanine_sha256`, the exact `ffmpeg_command` used, ffprobe-verified codec/duration/size, and wall-clock elapsed.

**Skip-if-exists contract**: when an output file and its sidecar already exist, and both the mezzanine hash and `preset_spec_version` match, `export()` returns the existing manifest without re-transcoding. Pass `--force` to override. Bumping `PRESET_SPEC_VERSION` in code invalidates existing exports for subsequent runs (without touching the files on disk).

**Mezzanine-as-source-of-truth**: `export()` reads `mezzanine.mp4` and never touches `compose.json` beyond reading `duration_sec` for progress math. Changing export format is a transcode pass, never a re-render.

## Known gotchas
- **Two worker replicas by default.** `compose.yml` sets `deploy.replicas: 2`.
  With `docker compose up -d worker` this yields `reelforge-worker-1` and
  `reelforge-worker-2`. Fine for Phase 0 because jobs are stateless, but keep
  it in mind when debugging log streams.
- **Anthropic reachability check** hits `client.models.list(limit=1)` and caches
  the result for 60 s. If `ANTHROPIC_API_KEY` is missing, `anthropic_reachable`
  is `false` and `/health` still returns 200 with `status: "degraded"`.
- **No GPU by default.** Apple Silicon and AMD GPUs run CPU-only. For NVIDIA,
  add `-f compose.gpu.yml` to the compose command and make sure the NVIDIA
  Container Toolkit is installed on the host.
- **Web dev hot reload** mounts `./apps/web` into `/app` and uses anonymous
  volumes for `node_modules` and `.next` so the host never overwrites the
  container's installed deps / build cache.
- **Whisper model download on first run.** The first `analyze` with a given model
  downloads weights into the `whisper_models` named volume (base.en ≈ 150 MB;
  large-v3 ≈ 3 GB). Don't interpret a slow first run as a hang — it's baked
  into the CTranslate2 flow and there's no intermediate progress surface.
- **Apple Silicon / macOS hosts** run Docker via a Linux VM with no GPU
  passthrough. faster-whisper is CPU-only + int8 on Macs; `base.en` is the
  right default. `large-v3` works but is slow (~2× real-time on M-series).
- **HDR footage** (color_transfer in `{smpte2084, arib-std-b67}`) auto-bumps
  the scene-detection threshold from 27.0 → 40.0 unless the user overrides it.
  Logged at WARNING so it's visible in `docker compose logs worker`.
- **SQLite WAL on a bind mount** creates `reelforge.db-wal` and
  `reelforge.db-shm` next to the DB. `.gitignore` excludes both.
- **NEVER open /data/reelforge.db from the macOS host while containers run.**
  WAL mode requires all connections to share one kernel's shm mapping;
  host-side reads through Docker's VirtioFS corrupted the DB on 2026-08-14
  ("database disk image is malformed"). Inspect via
  `docker exec reelforge-api-1 python -c ...` instead. Recovery if it ever
  recurs: stop everything, `sqlite3 db ".recover" | sqlite3 new.db`, swap in.
- **Two worker replicas** share `/data/reelforge.db`. WAL mode handles
  concurrent reads; writes are serialized per-connection by SQLite. That's
  fine for this workload (semantics upserts are rare and small).
- **Anthropic egress.** Every semantics call goes through `api.anthropic.com`.
  Behind a TLS-intercepting proxy, bake the corporate CA bundle into the image
  rather than disabling verification. `/health` surfaces reachability already.
- **Ranking determinism is not byte-identical at `temperature=0`.** Claude at
  temp=0 is *usually* deterministic but back-to-back calls can still differ in
  surface phrasing (e.g. different titles for the same span). The stable check
  is `--resume` (reuses `ranking_raw.json`). Downstream phases consuming
  `reels.json` should not depend on exact string identity across runs.
- **`.env.example` is a template, not a credential store.** Keep it filled with
  placeholders. A real `ANTHROPIC_API_KEY` in `.env.example` will leak to git
  on first commit.
- **Bundled music tracks are synthesized placeholders — but a curated CC0
  library now ships via `/data/music/`.** `scripts/fetch_cc0_music.py`
  downloads 11 real CC0 tracks from OpenGameArt (verified licenses, one+ per
  mood) into `/data/music/` and writes the user manifest. Run it with
  `docker compose run --rm -T --entrypoint python cli - < scripts/fetch_cc0_music.py`.
  `select_track` prefers `source == "user"` tracks over bundled placeholders
  whenever the mood is covered, so the placeholders only play on a fresh
  install that hasn't fetched the library.
- **Music packs + CC-BY auto-credit.** `scripts/fetch_music_packs.py`
  replaces the OpenGameArt pack (retires `*-oga-*` ids) with Scott Buckley
  (CC-BY 4.0, mp3 URLs scraped from track pages at run time) and Loyalty
  Freak Music (CC0, Internet Archive `archive.org/download/...`, license
  verified via the IA metadata API). `publish/credits.py::
  music_credit_for_reel` reads compose.json's `chosen_music`; when license
  starts with CC-BY the worker appends the credit to `pub["description"]`
  BEFORE platform dispatch, so YouTube descriptions and IG/TikTok captions
  all carry it (idempotent — `append_credit`). Pixabay/Mixkit forbid
  scripted downloads: hand-picked tracks go through the reel page's
  "Manage library" dialog (`music-library-dialog.tsx`) → existing
  `POST /music/uploads`. FMA gates downloads behind login — don't script it.
- **Final-mix loudness normalization is on by default.**
  `ComposeConfig.normalize_loudness` adds a `loudnorm I=-14:TP=-1.5:LRA=11`
  pass on the mixed bus (after amix / voice passthrough) followed by
  `aresample=48000` (loudnorm internally emits 192 kHz). -14 LUFS is the
  YouTube/TikTok/Spotify normalization target. Per-stem loudnorms still set
  the voice/music *balance*; the bus pass pins the final level.
- **Karaoke captions are TikTok-style word-highlight.** Words are grouped
  into short lines (`CaptionStyle.karaoke_max_chars`, default 18); the whole
  line stays on screen and the spoken word is re-colored via an inline
  `\c` override + bold. One Dialogue per word interval, back-to-back times so
  the line never flickers; intra-line pauses hold the current highlight.
  Implemented in `compose/captions.py` (not libass `\k` tags — timing stays
  under our control). The web compose panel defaults to karaoke.
- **Beat-synced transitions.** `ComposeConfig.beat_sync` (default on): the
  chosen music track is analyzed for tempo AND phase
  (`compose/beats.py::detect_beats` — onset flux + autocorrelation, prefers
  the finer tempo octave), then interior clips are shortened by ≤
  `beat_sync_max_adjust_sec` (0.45s) so each crossfade midpoint lands on a
  beat (`compute_beat_end_trims`, sequential — earlier trims shift later
  transitions). Trims flow into `extract_clips(end_trims=...)` AND
  `build_captions(end_trims=...)` — captions build a source→mezzanine
  segment map from the ACTUAL clip bounds, so any new bound-changing feature
  must thread through both.
- **Auto-reframe (subject-tracked crop).** `EffectsConfig.reframe`
  (default "auto"): portrait/square targets from wider sources get a
  subject-following crop instead of letterboxing. `compose/reframe.py`
  samples 12 frames per clip with OpenCV (motion-diff centroid + Haar face
  detection, faces weighted 3x), yielding a start/end x-center; the clip
  filter pans linearly between them (crop x is a t-expression — cheap).
  Falls back to centered crop on any failure; `reframe: "letterbox"`
  restores the old bars.
- **Ken Burns is scale+animated-crop, not zoompan.** zoompan re-resampled
  every frame (~10x slower renders); the current effect scales the clip up
  once by `ken_burns_zoom` and drifts a target-sized crop window
  diagonally. Do not reintroduce zoompan.
- **Editable timeline (post-generation editing).** `Reel.edit_json` stores a
  `ReelTimeline` (ordered `TimelineShot`s — arbitrary `[in,out]` ranges of
  ANY project video or photos — plus per-cut `transition_after` and
  `TextOverlay`s in mezzanine seconds). `GET/PUT/DELETE /reels/{id}/edit`
  (GET returns the AI cut as a default timeline when nothing is saved, plus
  the project's sources with scene thumbnails). The compose endpoint injects
  the saved timeline (paths resolved from asset ids) unless
  `ignore_edits: true`; `ComposeConfig.timeline` then REPLACES scene-derived
  shots, trim offsets and photo_inserts. Pipeline: `extract_timeline_clips`
  (multi-source, speech-snap on outer bounds, beat trims on interior),
  `resolve_transitions` gives per-cut (kind, duration) and `_xfade_offsets`
  / `compute_beat_end_trims` / captions all take per-cut lists. Captions are
  built per shot from THAT shot's asset transcript (`analyses` dict) — two
  shots from different assets may have overlapping source times. Overlays
  render through the same ASS file (`Overlay` style, layer 1, inline
  `\an`/`\fs`/`\c`/`\fad`), so `captions_for_render` is decided by
  `has_dialogue()` — not by caption mode. UI: `/reels/{id}/edit` page with a
  **scrubbable client-side preview** (`components/app/timeline-preview.tsx`):
  plays source footage straight from `GET /assets/{id}/media` (Range
  streaming), one `<video>` per distinct asset stacked + opacity-crossfaded
  on a wall-clock master timeline, photos as `<img>`, overlays as DOM text
  scaled by frameHeight/1920. `buildSegments()` mirrors the render graph's
  xfade placement. Not previewed: captions, music, LUT, reframe. HEVC
  sources can't decode in Chrome — surfaced as an in-frame warning.
- **Audio controls + voiceover.** `TimelineShot.volume/muted` →
  `ClipInfo.volume` → a per-clip `volume=` in the graph's audio prep (before
  the crossfade chain). Voiceovers are `Asset.kind="audio"` rows (ingest now
  accepts audio-only media: `video_codec="none"`, `MediaAsset.is_audio`)
  uploaded via multipart `POST /projects/{id}/voiceovers` from the browser's
  MediaRecorder (webm/opus in Chrome, mp4/aac in Safari — ffmpeg decodes
  both). `ReelTimeline.voiceovers` = takes placed at mezzanine `start_sec`.
  Render bus: takes → adelay+gain → amix → loudnorm(-12) = `[vo_ready]`;
  footage audio `sidechaincompress`-ducks under it (config
  `voiceover_ducking`), amix(normalize=0, duration=first) → `[voice_pre]` →
  existing voice loudnorm — so music still ducks under footage+voiceover
  together. Editor: "Record at playhead" plays the preview SILENCED
  (`silenced` prop) so the mic doesn't capture footage; takes are placed
  where recording started and can be nudged. Preview caps gain at 1.0 (HTML
  media limit) while the render allows up to 3x.
- **Natural-language selection prompt ("Direction").** `SelectionConfig.prompt`
  (max 500 chars, whitespace-stripped, ""→None) steers ranking: `rank.py::
  build_system_prompt` appends a USER DIRECTION block to `SYSTEM_PROMPT_V1`
  and `build_ranking_tool` adds a required `prompt_relevance` (0-100) tool
  field. Overall becomes `0.45*relevance + 0.55*scores.weighted`; candidates
  under `PROMPT_RELEVANCE_FLOOR` (35) are dropped BEFORE dedup (strict filter
  — zero matches → honest empty reels.json). Style intent rides
  `suggested_mood`, which already drives transitions/LUT/music. The resume
  stamp gains a `prompt` key ONLY when set (old stamps keep matching; any
  prompt change forces a fresh rank). No prompt → byte-identical old behavior
  (guarded by `test_no_prompt_is_unchanged`). `prompt_relevance` is persisted
  on the Reel row (additive migration) and surfaced through all three reel
  serialization paths + a "Match NN%" badge; the select endpoint 422s on
  invalid config (`INVALID_CONFIG`) instead of 500. CLI: `--prompt`.
- **Voiceover auto-captions + waveforms.** At the captions stage the compose
  pipeline transcribes every unmuted `VoiceoverTake` with faster-whisper
  (`analysis/transcribe.py::ensure_take_transcript`, cached at
  `/data/working/{take_asset_id}/transcript.json` + `.stamp` keyed on
  `(model, mtime)`; `ComposeConfig.voiceover_whisper_model`, default
  `base.en`) and passes `voiceover_captions=[(take, transcript)]` to
  `build_captions`. Take words are emitted at `take.start_sec + word.start`
  on the mezzanine timeline (no source→mezz mapping); footage words whose
  mapped midpoint falls under a take are suppressed so two caption streams
  never overlap. `CaptionStyle.caption_voiceover=False` turns it off.
  `GET /assets/{id}/waveform?start&end&buckets` returns a normalized peak
  envelope (ffmpeg → 8 kHz mono s16le → per-bucket max / 98th pct; in-memory
  LRU, 900 s span cap) — `components/app/waveform-bar.tsx` draws it under
  every shot and take in the editor with the preview playhead
  (`TimelinePreview.onTimeChange`, ~15 Hz). Note the compose POST body IS
  the `ComposeConfig` (no `config:` wrapper — unknown top-level keys are
  silently ignored).
- **Photos are shots, not sources.** An uploaded image becomes an Asset with
  `kind="photo"` (detected by `ingest.is_photo_probe` — image container /
  codec). Photos are never analyzed or selected; they're inserted at compose
  time via `ComposeConfig.photo_inserts` (`PhotoInsert.position` indexes the
  shot sequence: 0 = before the first clip, N = after the Nth). The API
  resolves `asset_id` → `path` at enqueue so the compose pipeline needs no DB
  access. `compose/photos.py` renders each still into a normalized clip with
  a silent 48 kHz stereo bed (the xfade/acrossfade chain needs an audio
  stream on every shot) and bakes the Ken Burns drift in — so
  `graph_builder` skips its own Ken Burns for `clip.is_photo`.
  **Anything that changes the shot list must flow into captions**:
  `build_captions(clips=...)` builds the source→mezzanine map from the actual
  ordered shots, and photo shots advance mezzanine time while mapping to no
  source span.
- **FFmpeg 5.1 here can't decode HEIC/HEIF.** iPhone photos in that format
  are rejected client-side with export instructions, and `complete_upload`
  turns any probe failure into a 400 (binning the assembled temp file)
  instead of a 500.
- **Speech-safe outer cuts.** `ComposeConfig.speech_safe_cuts` (default on):
  the reel's first/last cut points are snapped off the middle of spoken words
  via `compose/speech_snap.py` — extend up to
  `speech_safe_max_nudge_sec` (0.6s) to include the partial word, else drop
  it. Applied in `clips.py::clip_bounds` (pure — also folds in trim offsets)
  and mirrored by `captions.py` so caption timing tracks the ACTUAL rendered
  bounds (this also fixed captions drifting when trim offsets were used).
  Interior scene boundaries are never snapped — crossfades preserve words
  across contiguous scenes.
- **Edit Quality v1 invariants.** (1) Shot durations feed TRIPLICATED xfade
  math — `graph_builder._xfade_offsets`, captions' reclaim loop,
  `compute_beat_end_trims` — thread all three for any duration-changing
  feature, and run `clamp_transitions` on whatever list captions/beat-sync
  will consume. (2) Any new per-shot extraction parameter (like `speed`)
  goes into BOTH clip cache keys (clips.py scene + timeline). (3) Speed uses
  output-side seeking on post-setpts timestamps: `-ss/-to` and the reframe
  pan window scale by 1/speed; speed≠1 shots render muted with captions
  suppressed. (4) >6 clips render hierarchically (chunks of ≤5) — a 12-clip
  single-pass 1080x1920 xfade chain OOM'd at ~6 GB. (5) Style grammars
  (compose/styles.py) engage only in the smart-auto flow or when explicit;
  director proposals (compose/director.py) are validated against
  STYLE_BOUNDS or reverted per-entry — the director must never be able to
  fail a render. Director stamp = fingerprint(plan, style, model, d1) in the
  reel dir; unchanged re-composes are zero-token.
- **Only libx264 + AAC.** No NVENC, no hardware encode. Determinism and
  quality come first; we can revisit in Phase 7 after measuring.
- **Do not use `-c copy` for clip extraction.** Stream-copy with `-ss` snaps to
  the previous keyframe (up to 10 seconds of scene start lost). `extract_clips`
  always re-encodes. Same rule for anything downstream that seeks into the
  source.
- **FFmpeg stream fan-out requires `asplit` / `split`.** Using a labelled
  stream as input to two filters without splitting it gives the infamous
  "matches no streams" error at runtime. The graph builder splits `[voice]`
  before sidechain + amix — if you add another consumer, add another split.
- **Ken Burns applies per low-energy scene.** `visual_energy == "low"` in
  `analysis.semantics` triggers a slow zoompan for that clip. With
  solid-color test fixtures the model often tags scenes as low-energy, so
  expect zoompan in test renders.
- **zoompan is the single slowest filter in the pipeline.** It upsamples
  each frame to the target resolution internally; three 15s 1080p clips
  with zoompan can push render time from ~1 min to 10+ min on a laptop.
  If render feels stuck, check for zoompan; `--no-effects` or simply not
  having `visual_energy == "low"` scenes avoids it.
- **ProRes `profile:v` numbers are easy to miscopy.** `2` = 422 Standard
  (fourcc `apcn`), `3` = 422 HQ (fourcc `apch`). Swapping them produces a
  file that still plays but is the wrong grade. The preset registry test
  `test_prores_profiles_differ` guards this.
- **H.265 MP4 needs `tag:v hvc1`.** The default FFmpeg tag `hev1` plays in
  VLC and Chrome but silently fails in Safari/QuickTime. `mp4_h265_hq`
  sets `tag:v hvc1` explicitly.
- **Exports are not deleted on verification failure.** When `verify_export`
  raises, the broken output file stays on disk so the user can forensically
  ffprobe it. Keep this behavior — it's the difference between a 2-line
  error message and "what was it actually producing?"
- **Export output size floor is 64 KB.** Real 30s+ reels far exceed this
  even at aggressive CRF, but synthetic solid-color test fixtures can land
  under 1 MB. The floor is a correctness guard against zero/near-zero
  output, not a size-quality proxy.
- **Disk usage.** ProRes HQ of a 60s reel can be ~400-500 MB on real
  content. `/data/outputs/` grows quickly across many reels. Phase 7 will
  add cleanup tooling; for now users can `rm -rf ./data/outputs/<asset_id>/<reel_id>/*.mov`
  if they only need the MP4 presets.
- **API: settings are read once at import.** `Settings()` is constructed as a
  module-level singleton in `apps/api/settings.py`. Env-var overrides must be
  set before that import (compose's `env_file:` handles this in production).
  Tests mutate `settings_mod.settings.data_dir` directly to point at a tmp
  dir without re-importing.
- **API: `NullPool`, not `StaticPool`, for SQLite async.** `StaticPool`
  shares one connection across tasks which breaks concurrent FastAPI
  requests (reads see stale / uncommitted writes). `NullPool` opens a fresh
  connection per checkout; WAL + `busy_timeout=5000` handles the contention.
- **`DELETE /assets/{id}` aborts in-flight jobs, then cascades.** Order
  matters: abort arq jobs (needs `allow_abort_jobs = True` on the worker,
  else it's a harmless no-op) → mark them failed → delete publications →
  exports → reels → jobs → upload_sessions → repoint `project.source_asset_id`
  → delete the asset row → `rmtree` working/outputs + the upload file off the
  event loop. Deleting mid-analysis is the common case (clip too big/wrong),
  so skipping the abort leaves FFmpeg burning CPU on a deleted file.
- **API: `DELETE /projects/{id}` leaves files on disk.** DB rows are
  cascaded manually (SQLite FK DELETE CASCADE isn't reliable across SQLModel
  versions without explicit DDL). Files under `/data/working/...` and
  `/data/outputs/...` are NOT deleted. Full wipe = `rm -rf ./data/working
  ./data/outputs` on the host.
- **API: no Alembic.** Phase 5 uses `SQLModel.metadata.create_all()` plus a
  one-shot legacy-jobs-table drop. When schema evolution matters, layer
  Alembic in (Phase 7 polish).
- **API: `FileResponse` can't do Range well on multi-GB files** in some
  Starlette/FastAPI versions. `apps/api/streaming.py::stream_file_with_range`
  is the explicit helper every media endpoint should use.
- **API: SSE proxy buffering.** The `/jobs/{id}/stream` handler sets
  `X-Accel-Buffering: no`. Default Nginx buffers `text/event-stream` without
  this header; irrelevant in local dev compose, matters in Phase 7 prod.
- **API: caption-preview is the one sync FFmpeg call in the API.** It's
  time-boxed to 10s and rate-limited to 30 requests/minute per client IP
  (in-memory bucket — fine for a single-user app). Everything else enqueues.
- **Web: `NEXT_PUBLIC_*` is inlined at build time, not runtime.** Compose
  passes `NEXT_PUBLIC_API_URL` via `build.args` AND as a runtime `environment`
  variable; only the build-time one actually lands in the client bundle.
  Changing the API URL requires a rebuild of the `web` image, not a restart.
- **Web: the browser is outside the Docker network.** `NEXT_PUBLIC_API_URL`
  must be the **host-visible** URL (`http://localhost:8001`), not the
  Docker-internal hostname (`http://api:8001`). The worker and API see each
  other via `api:8001`; the browser cannot resolve that.
- **Web: Next.js 14 params are plain objects, not Promises.** The spec
  draft's `params: Promise<…>` + `use(params)` pattern is Next.js 15. On 14,
  declare `params: { projectId: string }` and access directly. Using `use()`
  on a plain object raises `"An unsupported type was passed to use()"` at
  runtime.
- **Web: all pages are `'use client'`.** React Query hooks run in the browser
  so the full data-fetching stack is client-side. SSR would require a
  per-request API client with proxied fetch, which has no value for a
  single-user tool. curl against a page URL returns the Next.js shell (~8 KB)
  plus the client bundle — actual UI content appears after hydration.
- **Web: `z.array(...).default([])` breaks TypeScript strict typing with the
  client's `api<T>({ schema })` helper.** The default makes input optional
  but output required, so `z.infer<>` stops matching the explicit generic.
  Keep Zod fields required if the API always emits them.

## Phase 0 acceptance criteria (passing as of this commit)
- `docker compose build` succeeds on a clean machine.
- `docker compose up -d redis api worker` → all three healthy within ~30 s.
- `curl http://localhost:8001/health` returns 200 with the documented shape.
- `./reelforge probe /data/inbox/<file>` prints the probe table.
- `docker compose down` leaves `./data` and `whisper_models` intact.

## Phase 7.2 acceptance status — smart auto-direction
The compose pipeline now picks transitions, color grade, and music from
the reel's `suggested_mood` instead of hard-coded defaults. The user can
override by flipping the "AI auto" toggle off (or by supplying explicit
non-"auto" values via the API directly).

- **Sentinels.** `ComposeConfig.transition.kind` accepts `"auto"`,
  `EffectsConfig.lut` accepts `"auto"`, and `ComposeConfig.smart_mode`
  (default `True`) controls whether those sentinels are resolved.
- **Resolver.** `packages/core/reelforge_core/compose/auto.py`:
  - `TRANSITION_BY_MOOD` — 10-mood → xfade kind lookup
    (energetic → slideleft, joyful → dissolve, calm/neutral → fade,
    somber/mysterious/melancholic → fadeblack, romantic → dissolve,
    triumphant → slideleft, tense → fade).
  - `LUT_BY_MOOD` — 10-mood → bundled LUT id (calm/romantic → warm,
    somber/mysterious/melancholic → cool, joyful/energetic → vivid,
    tense/triumphant → cinematic, neutral → none).
  - `resolve_smart_config(config, reel, analysis)` returns a *new*
    `ComposeConfig` with sentinels replaced. Explicit non-auto values
    always win over auto, even when smart_mode is on.
  - `pick_transition_kind_for_montage(child_moods)` chooses one xfade
    kind for the montage's chapter boundaries based on the dominant
    chapter mood (alphabetical tie-break for determinism).
  - `describe_smart_picks(resolved, reel)` returns a flat dict surfaced
    to the UI/manifest so the user sees what the AI did.
- **Wiring.** `compose/pipeline.py::compose()` calls
  `resolve_smart_config` at the very start so `compose.json.config`
  reflects exactly what rendered. `apps/api/routers/montages.py` passes
  the dominant-mood transition kind to `compile_montage_job`, which
  threads it through `compile_montage` → `build_montage_command`.
- **LUTs bundled at build time.** `assets/luts/synthesize_luts.sh`
  generates 4 small 17-point `.cube` files (warm / cool / cinematic /
  vivid) into `/app/assets/luts/` during the Docker build — no binary
  blobs in git. The graph builder resolves the id to a path via
  `compose.pipeline.resolve_lut` and applies `lut3d` after captions.
- **UI.** Compose panel at `apps/web/app/projects/[projectId]/reels/
  [reelId]/page.tsx` has an "AI auto" toggle (default on) that hides the
  transition / music / effects controls and shows a 4-row preview of the
  planned picks (transition / color grade / music / effects). When off,
  the manual controls render and the body sends `smart_mode: false` +
  explicit values.
- **Tests.** `tests/compose/test_auto.py` (15 tests) covers each picker
  (every mood maps to a known xfade kind / LUT id), the montage
  dominant-mood + tie-break behavior, `resolve_smart_config` passthrough
  when `smart_mode=False`, sentinel resolution when on, explicit
  overrides winning over auto, and the `describe_smart_picks` shape.
- **Regression gate.** `pytest -q` in the test profile: **329 passed**
  in ~5 min (was 293 in Phase 7; +21 in Phase 7.1 multi-asset/montage
  suites, +15 in this auto-picker pass).

## Phase 7 acceptance status
What shipped in this pass (10 mini-phases from the spec, scoped by
leverage-per-risk):

- **§2 Cost controls.** `packages/core/reelforge_core/pricing.py`
  (`PRICING_AS_OF = "2026-04-22"`); `usage.py` writes to a new
  `anthropic_usage` table; worker records one row per completed
  analyze/select job. Endpoints:
  `POST /api/v1/assets/{id}/analyze/estimate`,
  `POST /api/v1/assets/{id}/select/estimate`,
  `GET /api/v1/projects/{id}/usage`, `GET /api/v1/usage`.
- **§3 Caches + presets + batch compose.**
  `packages/core/reelforge_core/cache.py` is a content-addressed LRU
  file cache, wired into `compose/clips.py` (keyed on
  `asset_id + scene + mtime + resolution/fps/audio`) and `compose/music.py`
  (keyed on `track_id + duration + volume_db`). Caps via env
  (`CACHE_CLIPS_GB=20`, `CACHE_MUSIC_GB=2`, `CACHE_PREVIEWS_GB=1`).
  `compose_presets` CRUD + `compose_batch` endpoint in `routers/admin.py`.
- **§4 Custom music upload.** `POST /music/uploads` (multipart, 30 MiB cap,
  ffprobe-verified), `DELETE /music/{id}` (user-only; 403 on bundled).
  Entries land in `/data/music/manifest.json`; the existing loader already
  merges bundled + user.
- **§5 Transcript override.** `transcript_overrides` table,
  `GET/PUT/DELETE /assets/{id}/transcript` endpoints. `build_captions` now
  imports `transcript_store.load_override_sync` and swaps the transcript
  on the fly before rendering the ASS file. *UI editor deferred.*
- **§6 Trim offsets.** `Reel.trim_start_offset_sec` +
  `trim_end_offset_sec` (additive ALTER migration), `PATCH
  /reels/{id}/trim` with ±2 s + 25 s-minimum guard, compose enqueue
  auto-injects the offsets into `ComposeConfig`, `extract_clips` honors
  them on first and last clips. *UI drag handles deferred.*
- **§10 Cleanup.** `GET /disk_usage`, `GET /projects/{id}/disk_usage`,
  `POST /projects/{id}/cleanup` with modes `safe | working | outputs |
  all`; `POST /cache/purge?kind=clip|music|caption_preview`; CLI:
  `./reelforge cleanup --project <id> --mode <mode> --dry-run`.
- **§11 Log redaction.** `reelforge_core/log_redaction.py` — installed at
  root on both API and worker startup. 3 new unit tests.
- **§8 Prod deploy scaffolding.** `compose.prod.yml`,
  `docker/nginx/nginx.conf` + `conf.d/reelforge.conf.template`,
  `.github/workflows/build-and-publish.yml` (multi-arch GHCR). SSE + Range
  location blocks disable nginx buffering explicitly. *Not run end-to-end
  on a real VPS in this environment — `docs/deployment.md` is the manual.*
- **§7 CI.** `.github/workflows/ci.yml` runs `pytest` via our
  `compose.test.yml` profile + builds the web image. Mock-Anthropic
  integration job is a commented placeholder.
- **§12 Docs.** README rewrite, `docs/architecture.md`,
  `docs/deployment.md`, `docs/troubleshooting.md`, `docs/benchmarks.md`,
  `CHANGELOG.md` (0.1.0 → 0.7.0).

**Deferred explicitly** (tracked in `CHANGELOG.md` → "Deferred"):
- Full transcript-edit UI (plumbing ships; editor page doesn't).
- Drag-to-trim UI handles on the scene timeline (plumbing ships).
- Single-user auth login/logout flow + middleware.
- Sentry SDK init (env var gate ships).
- Mock-Anthropic CI integration job.

**Regression gate** — `pytest -q` inside the test profile: **293 tests
passing in ~5 min** (147 baseline + 146 new coverage-pass tests added in
the "full E2E coverage" follow-up). Image sizes unchanged (worker/api/cli
1.67 GB, web 153 MB).

**Coverage pass.** After the "as much as you can realistically do" push,
combined coverage on `packages/core` + `apps/api` sits at **80%**
(up from 63% baseline). `packages/core` averages ~88% with every module
≥ 77% and most ≥ 95%. `apps/worker/*` at 0% and `apps/cli/main.py` at 19%
are intentionally excluded — both are structural code exercised by live
acceptance runs rather than unit tests. See `docs/coverage.md` for the
full breakdown + justification of every uncovered surface.

## Phase 7.1 — Multi-clip + long-form output

What shipped:

- **Multi-asset projects.** The `Project → Asset` relationship was already
  one-to-many in the DB; the UI now actually uses it. Each asset shows its
  own analyze state in a per-row card on the project page, and a "+ Add
  another clip" button reveals the existing uploader inline. Selection
  fires one `select_reels_job` per analyzed asset (the "Independent,
  merged" mode the user picked).
- **`GET /api/v1/projects/{id}/reels`.** Aggregates `reels.json` from every
  asset in the project, re-sorts by `overall_score`, assigns a
  `project_rank`. The reels page uses this in place of the per-asset
  endpoint.
- **`SelectionConfig.output_form`** with three values:
  - `short` (default) — current behavior, configurable min/max sec + top_k.
  - `long_single` — one wide span per asset around
    `long_target_duration_sec` ±15%. Enumerator auto-bumps
    `max_scenes_per_reel` to 60 so 5-minute spans across many short scenes
    aren't truncated.
  - `long_montage` — same enumeration as short; a separate compile step
    stitches the top-K reels into one long-form mezzanine.
  The UI exposes form + duration + count on the selection screen
  (`apps/web/app/projects/[projectId]/page.tsx::SelectionPanel`).
- **Montage compile pipeline.** `packages/core/reelforge_core/compose/montage.py`:
  takes N already-composed mezzanines, builds one xfade chain, runs ffmpeg,
  writes a `compose.json` next to the output. Each chapter keeps its own
  music + captions; we deliberately don't try to bridge tracks across
  chapters (deliberate; "highlight reel" feel).
- **Montage worker job.** `apps/worker/jobs.py::compile_montage_job` — uses
  the same throttled Redis progress writer as the other compose paths.
- **Montage API.** `POST /api/v1/projects/{id}/montages` creates a
  Reel row with `child_reel_ids_json` set + enqueues the compile job. The
  Reel row's `mezzanine_path` is pre-written so the UI shows the row
  immediately (with `mezzanine_ready: false` until the job completes).
  `GET /api/v1/projects/{id}/montages` lists them.
- **Reel model.** Added `child_reel_ids_json` column (idempotent
  `ALTER TABLE` migration). Montage Reels reuse the existing
  Phase-4 export pipeline unchanged.
- **UI.** New "Compose montage" panel on the project reels page; selects
  composed-and-ready reels via toggle buttons + a transition-duration
  slider; lists active montages above the regular reel list.

**Regression gate** — `pytest -q` inside the test profile: **314 tests
passing in ~5 min** (293 + 21 new). New tests cover:
- `SelectionConfig.effective_*` bounds for each `output_form`
- candidate enumeration honors the widened long_single bounds
- montage command builder: xfade offsets, single-input case, empty inputs,
  bitexact metadata, target resolution wiring, 3-input offset chain math
- API: project-level reel aggregation (sort + project_rank); montage create
  rejects missing reels / un-composed reels; happy-path montage creation
  produces a Reel row with `child_reel_ids` and a queued job.

**Not run in this turn**: live-stack smoke against `:8001` (the host's
port was held by an unrelated container). All wiring is verified via the
test suite. To smoke against the live stack on a free host:
`docker compose up -d` →
`curl -s http://localhost:8001/api/v1/openapi.json | jq '.paths | keys | map(select(contains("montage")))'`.

## Phase 6 acceptance status
- `docker compose build web` produces a 153 MB image (well under the 300 MB
  target). `docker compose up` starts the full stack with `web` depending
  on `api` being healthy; `web` has its own `wget` spider healthcheck.
- `NEXT_PUBLIC_API_URL=http://localhost:8001` flows through the
  `docker/Dockerfile.web` `ARG`/`ENV` pair and the compose `build.args`, and
  grep of the client bundles confirms it's inlined into the JS (required for
  the browser, which runs on the host, to reach the API at the host-mapped
  port — not at the Docker internal `api:8001`).
- Four routes render 200:
  - `/` → home / project list + "New project" dialog.
  - `/projects/{id}` → three-zone: source (uploader or AssetSummary) +
    analysis (CTA → JobProgress w/ analyze stepper → summary) + selection.
  - `/projects/{id}/reels` → ranked list with 3-thumbnail strip,
    title + hook + mood pill, score bars.
  - `/projects/{id}/reels/{reelId}` → native `<video>` at `/preview` for a
    composed reel + compose config panel + export grid (4 preset rows with
    Download / Re-export once they complete). Compose + export use
    `JobProgress` with SSE.
- CORS preflight from `http://localhost:3000` works against `api:8001`; the
  API echoes `Access-Control-Allow-Origin`. Browser-side `fetch` from the
  client bundle reaches the API directly (no proxy needed).
- Sub-300 MB image check passes; `<video>` uses Range via the existing API
  streaming helper from Phase 5 (already verified 206 + `Content-Range`).
- **Client-only rendering.** All four pages use `'use client'` directives so
  the React Query hooks can run in the browser. `curl` on a page URL returns
  the Next.js shell only (~8 KB); actual content hydrates once the client
  bundle loads. This is intentional — SSR-ing authenticated-ish API data has
  no value for a single-user local tool.
- **Pragmatic test-suite scope.** Phase 6 ships the production app with full
  acceptance via running curl + browser against the compose stack. Playwright
  E2E, Vitest unit coverage, and bundle-analyzer budget checks from the
  spec's §14-15 are deferred to polish passes — the hooks + uploader state
  machine are covered by TypeScript strict mode + Zod runtime validation,
  which catches the classes of bug the spec's tests were designed to guard.

## Phase 5 acceptance status
- 144/144 pytest green inside the test profile. 26 new Phase-5 tests: HTTP
  Range parser (exact/suffix/open-ended/unsatisfiable), project CRUD + error
  envelope shape + request-id header, upload contract (happy path through
  assembly + content-addressed asset_id, 413/400/404 error paths, chunk-size
  validation), and pipeline enqueue contracts including 409
  `JOB_ALREADY_RUNNING` + `ANALYSIS_NOT_READY` preconditions.
- Live E2E through the stack:
  - `POST /api/v1/projects` → 201 with UUID.
  - Chunked upload of the 1.2 MB Phase-3 fixture (1 chunk) → `POST /complete`
    yields the same `cdde18d5...` content-addressed id seen in Phases 2-4.
  - `GET /api/v1/assets/{id}/analysis` returns the full `analysis.json` from
    disk.
  - `GET /api/v1/assets/{id}/reels` hydrates 3 reels from `reels.json`,
    marking rank-1 (id `b4a8cdf0ab5b53e7`) as `mezzanine_ready: true`.
  - `GET /api/v1/reels/{id}/preview` with `Range: bytes=0-1023` →
    `206 Partial Content`, body exactly 1024 bytes,
    `Content-Range: bytes 0-1023/1606990`.
  - `POST /api/v1/reels/{id}/exports` enqueues, `GET /api/v1/jobs/{job_id}`
    polling yields `status=done`, export row auto-syncs `output_path` and
    `file_size_bytes` from disk.
  - `GET /api/v1/exports/{id}/download` → 200,
    `Content-Type: video/mp4`,
    `Content-Disposition: attachment; filename="vibrant-to-void-mp4_h264_social.mp4"`,
    1.21 MB payload matching the on-disk bytes.
  - `GET /api/v1/jobs/{job_id}/stream` (SSE) emits `progress` events during a
    `--force` export and closes with a final `done` event carrying the job
    result — one clean disconnect, no dangling generators.
  - OpenAPI schema at `/openapi.json` lists all 25 endpoints.
  - CORS preflight (`OPTIONS /api/v1/projects` with `Origin:
    http://localhost:3000`) returns
    `Access-Control-Allow-Origin: http://localhost:3000`.
- `/ready` returns `{"ready":true}` once DB + Redis are usable; `/health`
  reports `ffmpeg`, `redis`, `anthropic_configured`, `anthropic_reachable`.
- Interrupted-job recovery wired into the lifespan: any `queued|running`
  `jobs` row on boot is flipped to `failed` with
  `error_message="interrupted by restart"`.
- Image size grew 1.63 GB → 1.67 GB (+40 MB; sqlmodel + aiosqlite + aiofiles
  + fakeredis-dev). Worker and CLI still use the same image as the API.

## Phase 4 acceptance status
- 118/118 pytest green inside the test profile. 28 new Phase-4 tests: preset
  registry (the ProRes 422→profile-`2` vs HQ→profile-`3` regression guard
  matches the spec's intentional-bug planting), golden-string command
  assertions per preset, and 10 integration tests that run real FFmpeg +
  ffprobe on a fresh mezzanine (each preset produces the expected codec /
  pixfmt / fourcc; skip-if-exists doesn't re-transcode; `--force` does;
  mezzanine mutation correctly invalidates the skip; missing mezzanine
  raises `MezzanineNotFoundError`; unknown preset raises
  `PresetNotFoundError`; wrong-codec claim raises `OutputVerificationError`).
- Queued `./reelforge export <asset_id> --reel 1 --all` on the 44.2s Phase-3
  mezzanine produced four outputs in 2:50 total. ffprobe confirmed:
  - `mp4_h264_social.mp4` — 1.2 MB — h264 High @ 4.1, yuv420p, AAC 96 kHz.
  - `mp4_h265_hq.mp4` — 1.5 MB — hevc Main, **codec_tag=hvc1** ✓, yuv420p.
  - `mov_prores_422.mov` — 47.6 MB — prores **codec_tag=apcn** ✓, profile=Standard, yuv422p10le.
  - `mov_prores_hq.mov` — 47.7 MB — prores **codec_tag=apch** ✓, profile=HQ, yuv422p10le.
  (Sizes between 422 Standard and HQ are nearly identical on solid-color test
  content because ProRes hits its I-frame compression floor; on real content
  HQ is ~50% larger.)
- Skip-if-exists: second run of the same preset completed in ~2 seconds
  (container startup + redis round-trip + sidecar read). No transcode.
- `--force`: output mtime advances across runs, confirming re-transcode.
- Sidecar `{preset_id}.export.json` contains `preset_spec_version`,
  `input_mezzanine_sha256`, exact `ffmpeg_command`, plus verified codec /
  duration / file_size_bytes. A developer can copy-paste the recorded command
  into a shell and reproduce the output.
- Missing-mezzanine path surfaces `MezzanineNotFoundError` with the missing
  file path. Redis progress hash reaches `overall=1.0` or `stage=error` with
  ~1h TTL.
- No image-size regression (still 1.63 GB worker/api/cli, 152 MB web).
- **Manual play-in-QuickTime/VLC/Chrome/Safari check deferred** (Docker
  container can't render a UI). The acceptance bullets that require it are
  left for a user hardware check before calling Phase 4 fully shipped.

## Phase 3 acceptance status
- 90/90 pytest green inside the test profile. New coverage: graph DSL
  (serialize with/without args, duplicate-label rejection, escape semantics,
  full xfade string), clip command construction (9:16/16:9/1:1, silent source,
  HDR tonemap order), ASS captions (transcript slicing, mezz-timeline offset
  with xfade subtraction, brace escape, karaoke = 1 event per word), music
  selection (mood match, neutral fallback, no-music short-circuit, explicit id
  not-found, seeded deterministic pick across multiple matches), music prep
  command shape, final-render graph (xfade offset math, subtitles present/not,
  music filters present/not, Ken Burns for low-energy, +bitexact flags), and
  **two integration tests that run real FFmpeg to completion**: happy-path
  compose of a synthesized 3-scene source → playable mezzanine at 1080×1920 /
  yuv420p / 30fps with the right duration; and determinism (same config +
  source → byte-identical mezzanine).
- Queued `./reelforge compose <asset_id> --reel 1 --no-effects --preset ultrafast`
  on the 120s / 3-reel fixture from Phase 2: produced a 1.6 MB mezzanine in
  25 seconds, ffprobe confirms h264 / yuv420p / 1080×1920 / 30fps / AAC
  stereo / 44.2s (matches 45s − 2×0.4 xfade), `compose.json` lists scenes
  [3, 4, 5] and `chosen_music: melancholic-01` (auto-matched to the reel's
  `suggested_mood`).
- **Queued byte-identical determinism confirmed end-to-end** across two
  back-to-back queued runs on the same asset/reel/config (SHA-256 match).
- Redis `job:{id}:progress` hit `overall=1.0` with TTL ~1h; worker JSON logs
  carry `job_id`/`asset_id`/`reel_id` for `compose_reel_job`.
- Image size grew from 1.61 GB → 1.63 GB (placeholder music). Inside the
  50 MB ceiling.
- **Ken Burns caveat (documented in known gotchas):** the default-on
  zoompan for low-energy scenes is extremely CPU-heavy — a 3-clip
  15s×1080p zoompan render stretches from ~25s to 10+ minutes on a
  laptop. Acceptance run used `--no-effects`. When compose feels stuck,
  suspect zoompan. Users will want `--no-effects` or a faster preset
  until this is sped up (potentially a Phase 7 optimization pass).

## Phase 2 acceptance status
- 58/58 pytest green inside the test profile. New coverage: candidate enumeration
  edges (10 × 5s, single 50s, all 2s with narrow vs wide `max_scenes`, all 90s,
  empty), overlap geometry (identity/subset/disjoint/partial), greedy dedup
  (identity tie, subset edge, disjoint pass-through), score formula
  (weights sum to 1, 0 and 100 endpoints), mocked-ranking scenarios (happy,
  no-candidates, missing-candidate retry, extra-id ignored, out-of-range score
  dropped, silent source, resume, determinism), and a Typer-CLI `select --local`
  smoke test.
- Queued `./reelforge select <asset_id>` on a 120s / 6-scene fixture produced
  11 candidates → 3 non-overlapping 45s reels with distinct content-specific
  titles and varied moods. Tokens in 5,760 / out 1,806; elapsed ~35s dominated
  by one Claude call.
- Empty-candidates path: `./reelforge select` on the 6s fixture prints the
  documented human message, writes `reels: []`, tokens 0 / 0, elapsed 0.01s,
  no Anthropic call made (confirmed by structured worker logs).
- `--resume` round-trips: byte-identical `reels.json` across two consecutive
  resume runs (excluding `created_at`/`elapsed_sec`).
- Redis `job:{id}:progress` reaches `overall=1.0`; TTL ~3500s at inspection.
- Structured JSON logs carry `job_id`/`asset_id` for both job kinds
  (`analyze_asset`, `select_reels_job`).
- No image-size regression (still 1.61 GB worker/api/cli, 152 MB web).

## Phase 1 acceptance status
- 30/30 pytest green inside the test profile, including an end-to-end pipeline
  run with a mocked Anthropic client that asserts non-identical moods across
  scenes.
- Queued `./reelforge analyze` flow verified on a 6s multi-scene fixture:
  scene detection (3 scenes), thumbnails, audio extraction, Whisper
  transcription, ebur128 loudness, Redis progress hash (stage + overall),
  SQLite job row transitions (running → failed on auth error, including error
  JSON), and 1 h TTL on the progress hash.
- **Pending real-API check:** the live "non-identical moods from Anthropic"
  assertion needs a valid `ANTHROPIC_API_KEY` in `.env`. With the default
  placeholder it short-circuits at a 401 and the worker job is recorded as
  failed with the error payload — which itself is a useful negative test.
- **Image size grew to ~1.6 GB** (was 754 MB in Phase 0). The growth is almost
  entirely faster-whisper + CTranslate2 + ONNX Runtime + scenedetect/opencv.
  The spec's Phase 0 threshold (< 1.2 GB) no longer applies once the ML stack
  is baked in; future image-slimming work belongs in a later phase if it
  becomes a real constraint.
