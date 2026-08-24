# 🎯 OCI Sniper v5.0.1

Always-Free instance grabber & manager for Oracle Cloud Infrastructure (OCI).

A self-hosted Flask web panel that hunts down OCI Always Free capacity (A1.Flex ARM / E2.1.Micro) with an automated retry loop, then lets you manage everything from one dashboard: instances, networking, firewall ports, quota usage — with Telegram alerts when an instance lands.

Formerly "OCI Provisioner". Same codebase, sharper aim: it sits scoped on your tenancy and fires the second free-tier capacity opens.

<a href="https://railway.com/deploy/oracle-cloud-instance?referralCode=MPoxuF&utm_medium=integration&utm_source=template&utm_campaign=generic">
  <img src="https://railway.com/button.svg" alt="Deploy on Railway" width="200">
</a>

## Features

- 🔁 **Auto-launch loop** — retries on `out of host capacity`, rotates availability domains, optional randomized delay; Telegram-pings you the moment an instance is provisioned
- 🖥️ **Full instance management** — list / reboot / delete single or DELETE ALL (type-to-confirm)
- 🌐 **Network bootstrap** — one click creates VCN + Internet Gateway + route + subnet if missing
- 🔓 **Firewall helper** — open ports via NSG or Security List with input validation (CIDR + port range)
- 📊 **Quota panel** — live Always Free usage: 2 A1 OCPUs / 12 GB RAM / 2 Micro / 200 GB boot volume
- 📡 **Telegram integration** — success/failure alerts + throttled live log streaming
- 🕙 **Keep-alive pinger** — built-in self-ping keeps free-tier hosts (Render etc.) awake 24/7
- 🔐 **Hardened auth** — Basic Auth (constant-time compare), per-IP rate limiting, fail-closed without password

## Quick deploy

### Render (free 24/7 with keep-alive)

1. Push this repo to GitHub → Render → **New Web Service** → connect repo
2. Environment: **Docker** (uses the included Dockerfile)
3. Add env var: `APP_PASSWORD` = your password
4. Deploy. Keep-alive auto-detects `RENDER_EXTERNAL_URL` and self-pings every 10 min so the service never sleeps.

### Railway

1. Connect repo → Railway auto-detects `railway.toml` / `nixpacks.toml`
2. Add env var `APP_PASSWORD`, deploy.

### Heroku

Uses the fixed `Procfile` (`web:` process). Set `APP_PASSWORD` in config vars.

### Docker anywhere

```bash
docker build -t oci-sniper .
docker run -d -p 5000:5000 -e APP_PASSWORD=changeme oci-provisioner
```

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
APP_PASSWORD=changeme python app.py
# http://localhost:5000
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | *(none)* | Basic Auth password for all API endpoints. **Set this in production.** With no password set, GET pages still render but every state-changing POST is refused (fail-closed). |
| `PORT` | `5000` | Listen port (platform-provided on Railway/Heroku/Render). |
| `MAX_ATTEMPTS` | `0` | Max provisioning-loop attempts before giving up. `0` = unlimited. |
| `RATE_LIMIT_WINDOW` | `60` | Rate-limit window in seconds (per IP). |
| `RATE_LIMIT_MAX` | `30` | Max requests per window per IP before 429. |
| `KEEP_ALIVE` | `true` | Self-ping daemon that keeps free-tier hosts (Render etc.) from sleeping. |
| `KEEP_ALIVE_INTERVAL` | `600` | Ping interval in seconds (min 60). 600s = every 10 min. |
| `KEEP_ALIVE_URL` | *(auto)* | Public base URL to ping. Auto-detected from `RENDER_EXTERNAL_URL` on Render; set explicitly on other platforms, e.g. `https://your-app.onrender.com`. |

## How to use

1. Open the portal, paste your OCI API key file contents (or upload the `.pem`) — or paste the whole `~/.oci/config`; user/tenancy/fingerprint/region are parsed automatically.
2. **Scan shapes → images → subnets** to populate dropdowns. No subnet? *Create subnet* bootstraps VCN + IGW + route table.
3. Pick shape (A1.Flex OCPU/memory or Micro), boot volume (50–200 GB), SSH public key → **Start provisioning loop**.
4. Watch the live terminal; get Telegram ping on success. Loop stops via **Stop** or `MAX_ATTEMPTS`.
5. Manage instances from the quota panel: reboot / delete single, or DELETE ALL (type-to-confirm).

> **Tip:** for A1.Flex, request the full 4 OCPU / 24 GB — if that's taken, drop to 2/12 and let the loop grab it. Both configs fit the free tier.

## Security notes

- All `/api/*` POST routes require Basic Auth (`APP_PASSWORD`). Constant-time comparison (`hmac.compare_digest`), rate-limited per IP.
- Fail-closed: no `APP_PASSWORD` set → every POST refused with 503.
- Private key lives only in your browser session, POSTed per-request over TLS, never written to disk by the app.
- "Remember account info" checkbox stores only non-secret OCIDs in localStorage — never the private key.
- Error responses sanitized/truncated (`safe_error_str`) so key material can't echo back through exceptions.
- Security headers on every response: `X-Frame-Options`, `nosniff`, HSTS, `Referrer-Policy`.

## API

All endpoints JSON. POST bodies take `user`, `tenancy`, `fingerprint`, `region`, `private_key` (+ endpoint-specific fields).

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness + version + keep-alive flag (no auth); first hit starts keep-alive pinger |
| GET | `/api/version` | App version (no auth) |
| GET | `/api/status` | Automation loop status |
| GET | `/api/logs?offset=N` | Incremental log fetch |
| POST | `/api/list-shapes` · `list-images` · `list-subnets` · `list-vnics` | Resource discovery |
| POST | `/api/create-subnet` | VCN+IGW+route+subnet bootstrap |
| POST | `/api/free-tier-status` | Quota usage (A1 OCPU/RAM, micro count, boot GB) |
| POST | `/api/test-launch` | Dry-run validation of image/subnet/shape |
| POST | `/api/auto-launch-loop` | Start retry loop |
| POST | `/api/stop-loop` | Stop retry loop |
| POST | `/api/open-firewall` | NSG/SecurityList rule add (validates CIDR + ports) |
| POST | `/api/scan-security-rules` | Dump effective rules for a subnet |
| POST | `/api/test-telegram` · `send-telegram` | Telegram bot check / manual message |
| POST | `/api/list-instances` · `delete-instance` · `reboot-instance` · `delete-all-instances` | Instance management |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Out of host capacity` forever | Normal for A1 in popular regions — leave the loop running; it grabs capacity the second it frees. Try all ADs. |
| Auth fails right after deploy | `APP_PASSWORD` not picked up — check platform env vars, redeploy. |
| Service sleeps anyway | Ensure `KEEP_ALIVE=true` and (non-Render) `KEEP_ALIVE_URL` points at the public URL. Check logs for `Keep-alive ping OK`. |
| Image dropdown empty | Your region may lack Ubuntu images for that shape — enable *all OS* mode or pick another shape. |

## Changelog v5.0 → v5.0.1

**Fixed**
- Railway/Heroku health check failing at boot: `python:3.12-slim` ships without timezone data, so
  `ZoneInfo("Asia/Phnom_Penh")` raised `ZoneInfoNotFoundError` during import and killed gunicorn before `/health` could answer.
- `app.py`: module-level tz lookups are now guarded (fall back to UTC); `tzdata` wheel added to requirements;
  Dockerfile installs the `tzdata` apt package; nixpacks setup adds `tzdata`.

## Changelog v4 → v5

**Fixed**
- Broken Procfile process name (`\web:` → `web:`) — Heroku builds failed silently
- Provisioning loop now honors `MAX_ATTEMPTS` env cap (was defined but dead code)

**Security**
- Basic Auth constant-time (`hmac.compare_digest`) instead of `==`
- Fail-closed when `APP_PASSWORD` unset — unauthenticated POSTs refused (v4 ran wide open)
- Per-IP sliding-window rate limiting (default 30 req/min) with `429` handler
- Error strings sanitized/truncated across all 15 API handlers — private-key material can't leak through exceptions
- Telegram live-log lines HTML-escaped before `parse_mode=HTML` send

**Input validation**
- `/api/open-firewall`: CIDR format, port range 1–65535, direction checked before mutating security lists
- Boot volume clamped 50–200 GB; Flex OCPUs/memory clamped to free-tier maxima (4/24)

**Robustness & UX**
- `/api/logs` tolerates garbage offset params; dev server threaded, debug off
- Optional localStorage persistence of account identifiers (never the private key)
- gunicorn access logs; `/health` reports version; new public `/api/version`

**Keep-alive**
- Built-in self-ping daemon (`KEEP_ALIVE=true`, default on): pings `/health` every 10 min so Render-style free tiers stay awake 24/7. URL auto-detected from `RENDER_EXTERNAL_URL`, else set `KEEP_ALIVE_URL`. Disable with `KEEP_ALIVE=false`.
