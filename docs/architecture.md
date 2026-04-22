# ReelForge architecture

## Service topology

```
       host:3000              host:8000
          │                       │
          ▼                       ▼
     +--------+   fetch     +-----------+    arq   +-----------+
     |  web   | ──────────▶ |    api    | ─────────▶|  worker   |
     | Next14 |             |  FastAPI  |           |  Python   |
     +--------+             +-----------+           +-----------+
                                  │                       │
                                  ▼                       ▼
                            +-----------+           +-----------+
                            |   redis   |           |  /data    |
                            | arq queue |           |  bind mnt |
                            +-----------+           +-----------+
                                                          │
                                                          ▼
                                               /models/whisper (named)
```

- **web** — Next.js 14 App Router + TypeScript. Dark-only UI, React Query,
  shadcn-style Radix primitives, chunked resumable uploader, SSE for job
  progress, native `<video>` for preview.
- **api** — FastAPI + SQLModel + aiosqlite. Owns SQLite (`/data/reelforge.db`);
  enqueues arq jobs; streams Range responses.
- **worker** — arq worker runs `analyze_asset`, `select_reels_job`,
  `compose_reel_job`, `export_reel_job`. All FFmpeg + Whisper + Anthropic
  calls happen here.
- **redis** — arq broker + ephemeral progress hashes (`job:{id}:progress`,
  1-hour TTL).
- **nginx** (prod only) — TLS termination, SSE-aware location blocks,
  `proxy_buffering off` for media routes. See `compose.prod.yml`.

## Data flow (happy path)

1. **Upload** — browser opens an upload session, streams 8 MiB chunks via
   `PUT /api/v1/uploads/{id}/chunks/{i}` straight to disk. `POST .../complete`
   assembles, runs ffprobe, computes a content-addressed `asset_id`.
2. **Analyze** — worker runs PySceneDetect → extracts audio once → Whisper
   (faster-whisper) → ebur128 → Claude Haiku per scene. Produces
   `analysis.json`.
3. **Select** — one batched Claude Sonnet call ranks 30-60 s candidate spans;
   greedy dedup; writes `reels.json`.
4. **Compose** — clips cache-check + re-encode → captions ASS → music prep →
   `filter_complex` (xfade + acrossfade + sidechaincompress + loudnorm) →
   `mezzanine.mp4` (H.264 yuv420p CRF 18, bit-exact).
5. **Export** — pure transcode from the mezzanine. Skip-if-exists by
   `(sha256(mezzanine), preset_spec_version)`. Produces MP4 H.264 / MP4 H.265
   `hvc1` / MOV ProRes 422 `apcn` / MOV ProRes HQ `apch`.

## Storage layout

```
/data/
├── inbox/                              # CLI path, unchanged
├── uploads/{asset_id}.{ext}            # API-written uploads
├── uploads/.parts/{upload_id}/         # chunk temp dir (purged on complete)
├── working/{asset_id}/                 # analysis + per-reel compose artifacts
│   ├── probe.json, analysis.json, reels.json, …
│   └── reels/{reel_id}/
│       ├── clips/, captions.ass, music.wav
│       ├── mezzanine.mp4 + compose.json
│       └── ffmpeg_commands.log
├── outputs/{asset_id}/{reel_id}/       # Phase 4 exports + sidecars
├── cache/clip/ , cache/music/ , cache/caption_preview/   # content-addressed
├── music/                              # user-uploaded music (manifest.json)
└── reelforge.db                        # SQLite (projects, assets, reels,
                                         # jobs, exports, usage, caches,
                                         # transcript_overrides, presets)

/models/whisper                         # named volume (~3 GB if you use large-v3)
/app/assets                             # bundled music placeholders + LUTs (baked)
```

## Phase boundaries

Each phase owns one artifact on disk and one set of endpoints. Downstream
phases **only read the canonical artifact** — they never re-run upstream work:

| Phase | Canonical output            | Read by                    |
|------:|------------------------------|----------------------------|
| 1     | `analysis.json`              | compose + select + API     |
| 2     | `reels.json`                 | compose + export + API     |
| 3     | `mezzanine.mp4` + `compose.json` | export + API            |
| 4     | `{preset}.mp4/.mov` + `.export.json` | API download       |

This is why re-running export doesn't recompose, and changing caption style
doesn't re-analyze.

## Determinism surfaces

- Clip extraction, mezzanine render, and exports all pass FFmpeg's
  `+bitexact` flags + zero the `creation_time` metadata. Given identical
  source bytes and config, output is byte-identical.
- LLM calls are at `temperature=0`, so Claude is *usually* deterministic —
  but not byte-guaranteed. The `--resume` paths (analyze + select) snapshot
  the LLM response to disk (`ranking_raw.json`, `semantics.json`) so
  re-processing is exactly reproducible when desired.

## Caches (Phase 7)

- **Clip cache** — `/data/cache/clip/`. Key =
  `sha256(asset_id, scene_idx, source_mtime, in_ts, out_ts, resolution, fps, …)`.
  LRU eviction capped by `CACHE_CLIPS_GB` (default 20 GiB).
- **Music cache** — same pattern. Key =
  `(track_id, duration_sec, volume_db)`. Default cap 2 GiB.
- **Caption-preview cache** — already in Phase 5. Cap default 1 GiB.
- Tracked in `file_cache` table (`cache_key`, `kind`, `path`, `size_bytes`,
  `last_accessed_at`).

## Anthropic usage tracking

Workers write one `anthropic_usage` row per completed LLM-using job
(analyze → semantics model; select → ranking model). The API aggregates by
project_id/job_id for the UI. See `packages/core/reelforge_core/pricing.py`
for the USD estimate table — these are local estimates only, *not*
authoritative billing.
