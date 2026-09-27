#!/usr/bin/env bash
# Create the named tunnel, point the subdomain at it, and write the config the
# container mounts. Run once:
#
#   cloudflared tunnel login        # you, in a browser — authorises the zone
#   ./scripts/named-tunnel-setup.sh
#
# Safe to re-run: it reuses an existing tunnel of the same name and overwrites
# the DNS record rather than erroring on the second pass.
#
# The subdomain is NOT created by hand in the Cloudflare dashboard. `route dns`
# writes a CNAME to <uuid>.cfargotunnel.com, which is a name that only resolves
# inside Cloudflare's edge — there is no address to type into a form, and a
# hand-made A or CNAME record on the same name collides with this one.
set -euo pipefail
cd "$(dirname "$0")/.."

TUNNEL_NAME="${TUNNEL_NAME:-reelforge}"
TUNNEL_HOSTNAME="${TUNNEL_HOSTNAME:-reelforge.blueprintautomation.tech}"
# The FastAPI service. Meta and TikTok fetch media straight off it.
TUNNEL_SERVICE="${TUNNEL_SERVICE:-http://api:8001}"
CERT="${HOME}/.cloudflared/cert.pem"
OUT="secrets/cloudflared"

command -v cloudflared >/dev/null || { echo "cloudflared is not installed (brew install cloudflared)" >&2; exit 1; }

if [ ! -f "$CERT" ]; then
  cat >&2 <<MSG
No Cloudflare certificate at $CERT.

Run this yourself first — it opens a browser and needs you to pick the zone:

  cloudflared tunnel login

Choose blueprintautomation.tech from the list.
MSG
  exit 1
fi

# --- the tunnel itself -------------------------------------------------------
# Reuse rather than recreate: the UUID is what the DNS record points at, so a
# second tunnel with the same name would leave the subdomain aimed at the old
# one and the failure would look like "the tunnel is up but nothing serves".
uuid=$(cloudflared tunnel list --output json \
  | python3 -c "
import sys, json
name = sys.argv[1]
print(next((t['id'] for t in json.load(sys.stdin) if t['name'] == name), ''))
" "$TUNNEL_NAME")

if [ -n "$uuid" ]; then
  echo "==> reusing existing tunnel '$TUNNEL_NAME' ($uuid)"
else
  echo "==> creating tunnel '$TUNNEL_NAME'"
  cloudflared tunnel create "$TUNNEL_NAME" >/dev/null
  uuid=$(cloudflared tunnel list --output json \
    | python3 -c "
import sys, json
name = sys.argv[1]
print(next((t['id'] for t in json.load(sys.stdin) if t['name'] == name), ''))
" "$TUNNEL_NAME")
  [ -n "$uuid" ] || { echo "tunnel was created but did not appear in the list" >&2; exit 1; }
  echo "    $uuid"
fi

creds="${HOME}/.cloudflared/${uuid}.json"
[ -f "$creds" ] || { echo "no credentials file at $creds" >&2; exit 1; }

# --- the subdomain -----------------------------------------------------------
echo "==> pointing $TUNNEL_HOSTNAME at the tunnel"
cloudflared tunnel --overwrite-dns route dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME"

# --- what the container mounts ----------------------------------------------
mkdir -p "$OUT"
cp "$creds" "$OUT/credentials.json"
chmod 600 "$OUT/credentials.json"

# Ingress is resolved top to bottom and the last rule must be a catch-all, or
# cloudflared refuses to start. Pointing at web:5175 rather than the API is
# deliberate: the Vite dev server proxies /api, /mcp and /media through, so one
# hostname carries the dashboard, the OAuth callbacks, the MCP endpoint Muse
# connects to, and the signed media URLs TikTok fetches.
cat > "$OUT/config.yml" <<CFG
tunnel: ${uuid}
credentials-file: /etc/cloudflared/credentials.json
metrics: 0.0.0.0:2000

ingress:
  - hostname: ${TUNNEL_HOSTNAME}
    service: ${TUNNEL_SERVICE}
  - service: http_status:404
CFG

echo "==> validating the ingress rules"
cloudflared tunnel --config "$OUT/config.yml" ingress validate

cat <<MSG

Tunnel ready.

  name      $TUNNEL_NAME
  uuid      $uuid
  hostname  https://${TUNNEL_HOSTNAME}
  config    $OUT/config.yml (credentials.json alongside it — gitignored)

Put these in .env, replacing any trycloudflare URLs:

  REELFORGE_PUBLIC_MEDIA_BASE=https://${TUNNEL_HOSTNAME}
  REELFORGE_PUBLIC_API_BASE=https://${TUNNEL_HOSTNAME}

Then bring the stack up on it:

  docker compose -f compose.yml -f compose.named-tunnel.yml up -d

MSG
