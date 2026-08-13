# Troubleshooting

A list of symptoms and the fixes that usually work. If a symptom isn't here,
start by checking `docker compose logs api worker | jq .`.

## Upload hangs or fails partway through

- **Symptom:** progress bar stalls; browser eventually times out.
- **Check:** reverse-proxy `client_max_body_size`. Our nginx config is set
  to `8G`. If you're using another proxy (Cloudflare tunnels are a common
  culprit), verify it doesn't cap request bodies below the per-chunk size
  (8 MiB default).
- **Check:** `docker compose logs api` for the `UPLOAD_TOO_LARGE` or
  `UPLOAD_CHUNK_OUT_OF_ORDER` error code. The envelope's `details` field
  tells you which size limit tripped.
- **Resume it:** reload the page; the uploader stores the session id in
  `localStorage` and pulls the canonical state from the API.

## Analyze is slow

- **Fast path:** use Whisper `base.en` (default). It's ~1 × real-time on
  CPU, ~4 × real-time on M-series via CT2 int8.
- **GPU:** if you have NVIDIA, run with `compose.gpu.yml`:
  `docker compose -f compose.yml -f compose.gpu.yml up -d worker`.
- **First run only:** the first invocation downloads the model (~150 MB for
  base.en, ~3 GB for large-v3) into the `whisper_models` named volume.
  Subsequent runs reuse it.

## "Job stuck in queued forever"

- **Check:** `docker compose logs worker`. A worker that isn't running
  won't pick up the job.
- **Check:** `docker compose exec redis redis-cli LLEN arq:queue:default`.
  Non-zero with no progress in logs = the worker exited.
- **Recover:** restart the worker. On API boot, any non-terminal job is
  flipped to `failed` with `error_message: "interrupted by restart"`.

## `job:{id}:progress` hash missing from Redis

- Keys have a 1-hour TTL. If you poll after that window, the API falls back
  to the DB snapshot (terminal-state-only). SSE just closes with the final
  event.

## ProRes export plays black in QuickTime

- **Root cause:** wrong pixel format or wrong profile. ProRes *requires*
  `yuv422p10le`. The preset registry test (`test_prores_uses_422_pixel_format`)
  guards this.
- **Check:** `ffprobe -show_streams output.mov | grep -E 'pix_fmt|codec_tag_string'`
  → must be `yuv422p10le` + `apcn` (422) or `apch` (HQ).

## Safari can't play the H.265 export

- **Root cause:** codec tag is `hev1` instead of `hvc1`. Safari only accepts
  `hvc1` for MP4 + HEVC.
- **Check:** `ffprobe -show_streams mp4_h265_hq.mp4 | grep codec_tag_string`
  → must read `hvc1`. Our `mp4_h265_hq` preset sets `-tag:v hvc1`
  explicitly.

## SSE progress stream stops updating

- **Behind a proxy:** nginx buffers `text/event-stream` unless you disable
  buffering. Our prod config sets `proxy_buffering off` +
  `X-Accel-Buffering: no` on the stream location. If you've put a second
  proxy in front (Cloudflare, etc.), check its buffering settings too.
- **Fallback:** the UI falls back to 1.5 s polling on any `EventSource`
  `onerror`. Check the browser console for the fallback log line.

## Compose feels slow (first run)

- **Fixed:** Ken Burns used to run through FFmpeg's `zoompan` (per-frame
  resampling — a 3-clip all-low-energy reel could take 10+ minutes). It's now
  a constant-zoom + animated-crop pan with negligible render cost. If a
  compose is still slow, check for HDR tonemapping and remember the clip
  cache makes re-composes with identical aspect/fps 5-20× faster.

## "This file is larger than the configured limit" on upload

- `MAX_UPLOAD_GB=5` in `.env` is the limit. Bump and restart the API. Your
  reverse proxy must also allow a body that large (`client_max_body_size`).

## `video/quicktime` download returns `application/octet-stream`

- Shouldn't happen with the shipped API (`Content-Type` is hard-coded from
  the extension in `routers/media.py`). If you see this, confirm the file
  extension on disk is actually `.mov`, not `.mp4`.

## Disk filling up

- `./reelforge cleanup --mode safe --dry-run` to see what's safe to delete
  (tmp dirs, part dirs, caches).
- `./reelforge cleanup --project <id> --mode working` to nuke analysis +
  compose artifacts for one project (forces re-analyze).
- `/api/v1/disk_usage` in the UI's settings dialog (Phase 7.x) shows the
  per-project breakdown.

## Bundled music sounds like a synthesizer, not real music

- Correct — Phase 3's bundled tracks are deterministic CC0 placeholders
  synthesized at Docker build time. See `assets/music/LICENSES.md`.
  Upload your own via the `<MusicPicker />` or `/api/v1/music/uploads`.

## Cost looks wrong in the UI

- The table in `packages/core/reelforge_core/pricing.py` is a **local
  estimate**, not a bill. Actual usage from the Anthropic console is the
  authoritative number. Check `PRICING_AS_OF` in that file to see how old
  the rates are.
