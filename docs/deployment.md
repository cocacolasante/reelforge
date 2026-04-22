# Deploying ReelForge to a single VPS

Target: Ubuntu 22.04 with Docker Engine ≥ 24 installed. Scope: one user,
one domain, HTTPS via Let's Encrypt, images pulled from GHCR.

## 0. Prerequisites on the host

- 4 vCPU, 8 GB RAM, 40 GB disk minimum. More is better — ProRes exports are
  hundreds of MB each.
- `docker`, `docker compose`, `curl`. No Python/Node runtime needed on the host.
- A DNS A record pointing at the VPS IP (e.g. `reelforge.example.com`).
- An **Anthropic API key**. Treat it like a credit card number.

## 1. Clone and configure

```bash
git clone https://github.com/<you>/reelforge.git /opt/reelforge
cd /opt/reelforge
cp .env.example .env
# Paste ANTHROPIC_API_KEY into .env.
# Optional: SENTRY_DSN, AUTH_MODE, AUTH_USERNAME, AUTH_PASSWORD_BCRYPT, AUTH_SESSION_SECRET.
```

Export the three prod-only vars (not in `.env`; keep them shell-scoped so
`docker compose` substitutes them in `compose.prod.yml`):

```bash
export GH_OWNER=<github-owner>       # e.g. your GitHub handle
export VERSION=v0.7.0                # tagged image to pull
export DOMAIN=reelforge.example.com
```

## 2. Bootstrap TLS

Let's Encrypt wants a reachable webroot. First boot nginx without the HTTPS
vhost so the ACME HTTP-01 challenge can complete. The template ships with
both vhosts defined, so we use a minimal bootstrap config for the first run:

```bash
# Render the vhost with your real domain
envsubst '${DOMAIN}' \
  < docker/nginx/conf.d/reelforge.conf.template \
  > docker/nginx/conf.d/reelforge.conf

# First, swap in a HTTP-only stub so nginx can boot before certs exist
cat > docker/nginx/conf.d/bootstrap.conf <<EOF
server {
    listen 80 default_server;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 404; }
}
EOF
mv docker/nginx/conf.d/reelforge.conf docker/nginx/conf.d/reelforge.conf.disabled

docker compose -f compose.yml -f compose.prod.yml up -d nginx

# Obtain the cert via the one-shot certbot container (webroot plugin)
docker compose -f compose.yml -f compose.prod.yml run --rm \
  certbot certonly --webroot -w /var/www/certbot \
  --email you@example.com --agree-tos --no-eff-email \
  -d ${DOMAIN}

# Re-enable the HTTPS vhost
rm docker/nginx/conf.d/bootstrap.conf
mv docker/nginx/conf.d/reelforge.conf.disabled docker/nginx/conf.d/reelforge.conf
docker compose -f compose.yml -f compose.prod.yml restart nginx
```

## 3. Bring the rest up

```bash
docker compose -f compose.yml -f compose.prod.yml pull
docker compose -f compose.yml -f compose.prod.yml up -d
```

Check readiness:

```bash
curl -fsS https://${DOMAIN}/api/v1/health | jq .
```

The certbot container is kept running; it loops every 12 h and calls
`certbot renew --webroot`. Reload nginx after a successful renewal:

```bash
docker compose -f compose.yml -f compose.prod.yml exec nginx nginx -s reload
```

(Or add a cron to call that once a week — safe to reload even when nothing
changed.)

## 4. Optional auth

Set in `.env`:

```
AUTH_MODE=single_user
AUTH_USERNAME=you
AUTH_PASSWORD_BCRYPT=<bcrypt hash>     # generate with `htpasswd -nbB you 'password'`
AUTH_SESSION_SECRET=<32-byte hex>       # generate with `openssl rand -hex 32`
```

Restart api after editing.

## 5. Upgrading

```bash
cd /opt/reelforge
git pull
export VERSION=v0.7.1
docker compose -f compose.yml -f compose.prod.yml pull
docker compose -f compose.yml -f compose.prod.yml up -d
```

Migrations run on API boot (the `create_all()` step + any additive `ALTER
TABLE` fix-ups). No manual steps.

## 6. Backup

**Files to snapshot:**

- `/opt/reelforge/data/reelforge.db` (and the `-wal` / `-shm` sidecars)
- `/opt/reelforge/data/working/` — only the final `analysis.json`,
  `reels.json`, `mezzanine.mp4`, `compose.json` are durable outputs. The
  `clips/` + `tmp/` subdirectories are re-generatable cache.
- `/opt/reelforge/data/outputs/` — the downloadable exports.

**Skip** `/opt/reelforge/data/cache/` — it's regenerable and can be large.

Simplest backup is an LVM/ZFS snapshot or rsync of `./data/` while the
containers are running (SQLite in WAL mode tolerates concurrent reads). For
a stricter snapshot:

```bash
docker compose -f compose.yml -f compose.prod.yml stop api worker
tar czf /backup/reelforge-$(date +%Y%m%d).tar.gz -C /opt/reelforge data
docker compose -f compose.yml -f compose.prod.yml start api worker
```

## 7. Logs

```bash
docker compose -f compose.yml -f compose.prod.yml logs -f api | jq .
docker compose -f compose.yml -f compose.prod.yml logs -f worker | jq .
```

Set `SENTRY_DSN` in `.env` to forward unhandled exceptions upstream. Leave
blank to disable (default).

## 8. Shutdown / wipe

```bash
docker compose -f compose.yml -f compose.prod.yml down      # keeps data
docker compose -f compose.yml -f compose.prod.yml down -v   # also drops the whisper models volume
rm -rf /opt/reelforge/data                                   # actually deletes user data
```
