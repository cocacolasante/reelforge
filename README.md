# ReelForge

Take a long video in. Get ranked short reels out, captioned and exported.

Everything — FFmpeg, Whisper, the API, the worker, the web UI — runs in Docker.
The only host requirements are Docker Desktop / Docker Engine ≥ 24, 8 GB RAM,
and 20 GB free disk.

```
       upload               analyze             select              compose            export
  +----------------+    +-------------+    +--------------+    +-------------+    +------------+
  |  Drop video →  | →  | scenes +    | →  | Claude ranks | →  | FFmpeg      | →  | MP4 / MOV  |
  |  chunked PUT   |    | transcript +|    |  30-60s reels|    | mezzanine   |    |  downloads |
  |  (resumable)   |    | semantics   |    |  + titles    |    | (H.264 CRF18)|    |            |
  +----------------+    +-------------+    +--------------+    +-------------+    +------------+
        web                 worker              worker              worker              worker
```

## Quickstart (15 minutes flat)

```bash
git clone https://github.com/<you>/reelforge.git
cd reelforge
cp .env.example .env
# Open .env and paste your ANTHROPIC_API_KEY.

docker compose up -d
open http://localhost:3000
```

Drop a video in, click through the flow, download the MP4. For the full
benchmarked walkthrough see [docs/benchmarks.md](docs/benchmarks.md).

## What the UI does

- **Projects** at `/` — one video per project.
- **Project detail** at `/projects/{id}` — three zones: upload, analyze, select.
- **Reels** at `/projects/{id}/reels` — ranked list with thumbnails, scores,
  hooks, and mood.
- **Reel detail** at `/projects/{id}/reels/{reelId}` — preview player +
  compose config (aspect, transition, captions, music, CRF) + export grid
  (4 presets including ProRes).

## CLI

Same pipeline via the CLI shim:

```bash
./reelforge probe /data/inbox/my.mp4
./reelforge analyze /data/inbox/my.mp4                  # queued via worker
./reelforge select <asset_id>
./reelforge compose <asset_id> --reel 1 --aspect 9:16
./reelforge export <asset_id> --reel 1 --all
./reelforge cleanup --mode safe --dry-run               # scopes: safe | working | outputs | all
```

## GPU (NVIDIA only)

```bash
docker compose -f compose.yml -f compose.gpu.yml up -d worker
```

Apple Silicon + AMD hosts run CPU-only (Whisper `base.en` is the right
default on Macs; `large-v3` works but is ~2× real-time).

## Development mode

```bash
docker compose -f compose.yml -f compose.dev.yml up
```

Bind-mounts source, runs `uvicorn --reload` and `pnpm dev`. Edit any
`.py`/`.tsx`, refresh.

## Tests

```bash
docker compose -f compose.yml -f compose.test.yml --profile test run --rm test pytest
```

## Deployment to a VPS

See **[docs/deployment.md](docs/deployment.md)** for the step-by-step on
Ubuntu 22.04: DNS, TLS via certbot, `compose.prod.yml`, upgrades, backup.

## Docs

- [docs/architecture.md](docs/architecture.md) — service topology, data flow,
  storage layout.
- [docs/deployment.md](docs/deployment.md) — remote deployment.
- [docs/troubleshooting.md](docs/troubleshooting.md) — common symptoms and
  fixes.
- [docs/benchmarks.md](docs/benchmarks.md) — cold-start-to-first-export timing.
- [docs/coverage.md](docs/coverage.md) — test coverage + what's intentionally uncovered.
- [CHANGELOG.md](CHANGELOG.md) — per-release summary.
- [CLAUDE.md](CLAUDE.md) — working notes for the development agent
  (also a useful reading order for humans).

## License

See `LICENSE` once added. The bundled music placeholders are CC0; bundled
fonts (Inter) are OFL-licensed.
