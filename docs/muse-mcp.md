# Muse / MCP agent access

Muse (Meta's assistant) reaches ReelForge as a **custom MCP connector**: a
hosted Streamable HTTP endpoint plus a bearer key. Meta doesn't review
custom connectors, so it works as soon as the URL is reachable and a key is
pasted in. Same design as the Muse integrations in
`~/Projects/socialgrowthagent` and `~/Projects/emailblaster`
(`docs/muse-mcp.md` there) — read those before changing this one.

## Connect Muse

1. ReelForge → **Agent access** (the key icon in the header) → create a key
   ("Muse on my phone"). Copy it — it is shown once; only a hash is stored.
2. In Muse, add a custom connector:
   - URL: `https://reelforge.blueprintautomation.tech/mcp`
   - Auth: bearer token = the key.
3. Ask Muse what footage you have.

The named tunnel must be running, or that URL is dead:

```bash
docker compose -f compose.yml -f compose.named-tunnel.yml up -d
```

The Agent access page shows the URL it would hand out and warns when it is
still `localhost`, which no phone can reach.

## What it can and can't do

| Can | Can't |
|---|---|
| List your projects and the footage in each (`overview`, `list_footage`) | Publish anything to YouTube, Instagram or TikTok |
| Hand you a link for sending footage from your phone (`start_upload`) | Upload the video itself — MCP tools carry JSON, never bytes |
| Cut short vertical clips from a batch, with a direction and a count (`cut_reels`) | Delete a project, a clip or a key |
| Build one long video mixed from every clip in a batch (`make_long_video`) | Start a second cut on a batch already being cut |
| Report progress in plain words and return the finished clips (`check_job`) | Edit a timeline, change captions, or re-render one clip |
| Deliver by link, synced folder, email, or any combination | See anything outside this ReelForge install |

Publishing is absent deliberately: posting stays in the dashboard, where you
watch the video first. The tool list is pinned by
`tests/api/test_mcp.py::test_tools_list_is_pinned` — widening it is a
deliberate edit there.

## The shape of a request

```
you → Muse          "cut some clips from the skimboard footage"
Muse → list_footage  finds the batch and its projectId
Muse → cut_reels     returns a jobId; nothing is rendered yet
Muse → check_job     "picking the best moments", 48% …
Muse → check_job     done: two clips, each with a link
```

Cutting takes minutes to tens of minutes — analysis alone runs about
real-time on a laptop. The tools are built around that: `cut_reels` and
`make_long_video` return a job id immediately and `check_job` reports the
stage in words ("watching the footage", "listening to what's said",
"rendering"). There are no callbacks into the agent; it polls.

Sending footage works the other way round, because an agent cannot carry
video: `start_upload` mints a signed link, you tap it and pick clips, and
they land in a named batch. Or put files in the watch folder and skip the
link entirely.

## How it works

- `POST /mcp` (`apps/api/routers/mcp.py`) — stateless JSON-RPC over
  Streamable HTTP (`initialize`, `ping`, `tools/list`, `tools/call`, batch
  arrays, 202 for notifications; plain JSON, never SSE; `GET`/`DELETE` →
  405). Protocol versions 2025-06-18 / 2025-03-26 / 2024-11-05.
- Auth: `Authorization: Bearer rf_…`. Keys (`api_keys`) are SHA-256 hashed,
  compared in constant time, revocable, and stamped with `last_used_at`.
  Twenty bad keys from one address in ten minutes gets a 429. Key management
  is dashboard-only — no tool can mint or revoke one.
- Tools (`apps/api/mcp/tools.py`) call ReelForge's own routes in-process with
  the caller's key, so validation, conflict checks and job bookkeeping apply
  unchanged. Refusals come back as tool output (`isError: true`, "ReelForge
  refused this: …"), never as protocol errors.
- Cutting (`apps/api/routers/agent.py` → `agent_cut_job` →
  `apps/worker/agent_cut.py`) runs analyze → select → compose → export as one
  job. Analysis is reused when it already exists, so a second cut of the same
  footage is much faster. One cut per project at a time.
- Delivery (`apps/worker/delivery.py`): links always, plus the folder and
  email channels when asked. A delivery failure reports itself and never
  fails the cut.

## Public origin (named Cloudflare tunnel)

`reelforge.blueprintautomation.tech` → tunnel `reelforge` →
`http://api:8001`, forwarding **only**:

| Path | Why it is public |
|---|---|
| `/mcp` | the agent endpoint; a bearer key is required |
| `/media/...` | signed, expiring links to finished clips |
| `/upload/...` | signed upload links, opened on a phone |
| `/public/media/...` | one export while Instagram fetches it |
| `/health` | liveness; no data |

Everything else 404s at Cloudflare's edge. **This is load-bearing**: the
ReelForge API has no session auth — it assumes localhost — so forwarding the
whole host would publish the dashboard API, including
`DELETE /api/v1/projects/{id}`, to anyone who guessed the hostname. It did,
until 2026-09-29. The dashboard is reached at `localhost:3000` and OAuth
callbacks use `REELFORGE_PUBLIC_API_BASE`, which is localhost, so nothing
needs the wider forward.

Re-run `./scripts/named-tunnel-setup.sh` to regenerate the config, then
restart the container — cloudflared reads its rules at startup, so editing
the file alone changes nothing.

## Links are capabilities

A media link is an HMAC over the file's path *relative to* `/data/outputs`,
plus an expiry (48h). `/media/{token}` sits outside every auth gate on
purpose: it has to work when tapped in a chat, on a phone that holds no key.
Containment is re-checked when a token is resolved, so a link can never name
the database or a source video. Upload links are the same mechanism with a
6-hour life and a project id inside.

The signing secret lives at `/data/.link_secret`, generated on first use, so
links survive a restart. Delete it and every outstanding link dies.

## Settings

| Variable | What it does |
|---|---|
| `REELFORGE_PUBLIC_MEDIA_BASE` | the public origin links are built on |
| `REELFORGE_WATCH_DIR` | folder to ingest footage from (must be mounted into the `api` container) |
| `REELFORGE_DELIVERY_DIR` | where `delivery: folder` copies finished clips |
| `REELFORGE_DELIVERY_DEFAULT` | `links`, `folder`, `email`, comma-separated |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_TO` | email delivery |

A folder on the Mac is only visible to the API container if it is mounted.
In `compose.yml` under `api.volumes`:

```yaml
      - ${REELFORGE_WATCH_DIR:-/dev/null}:${REELFORGE_WATCH_DIR:-/dev/null}
```

Mounting it at the same path inside the container keeps one value working on
both sides.

## Honest limits

- The server can describe a tool and refuse bad input, but it cannot see your
  chat. Whether Muse asks before starting an expensive cut is Muse's
  behaviour, not something enforced here.
- A key grants the whole install. ReelForge is single-user; there is no
  workspace to scope to. Revoke it from Agent access if a phone goes missing.
- Anyone holding a link can fetch that clip until it expires. That is the
  point of the link, and the reason they are short-lived.
