# OCI Provisioner Portal

A small Flask web UI for launching OCI Always Free instances with optional Telegram alerts. It is designed to run as **one Gunicorn worker** on Railway or a low-spec VPS.

## Deploy on Railway

1. Create a Railway service from this repository.
2. Set `APP_PASSWORD` in the service variables. This enables HTTP Basic Auth for the UI and API.
3. Deploy. Railway supplies `PORT`; the included Dockerfile and `railway.toml` use it automatically.
4. Use `/healthz` as the health endpoint if you configure Railway manually.

The container uses one worker and two threads. This is deliberate: the provisioning loop is process-local, and multiple workers would create separate status/log stores and could make the UI misleading. The OCI SDK is imported only when an OCI API operation is first used.

## Staying awake 24/7 (Railway sleeping)

Railway's **Serverless** feature (formerly *App Sleeping*) pauses a service when it stops
seeing **outbound** traffic for at least 5 minutes (in practice 5–10, sampled on an
interval). Inbound visits alone do not count — only traffic your service *sends* resets
the idle timer, so a quietly idling sniper gets paused and the provisioning loop dies
with it.

This app ships with a built-in keep-alive pinger that fixes that:

- On boot it starts a tiny background thread that sends an HTTP GET every
  `KEEPALIVE_INTERVAL_SECONDS` (default 240s) to `https://$RAILWAY_PUBLIC_DOMAIN/healthz`
  — this app's own public endpoint. Railway sets `RAILWAY_PUBLIC_DOMAIN` automatically
  once the service has a public domain, so **zero configuration is needed on Railway**.
- The outbound request resets Railway's idle timer every 4 minutes, well inside the
  ≥5-minute window, so the service (and the in-memory provisioning loop) stays up 24/7.
- Any other host works too: point `KEEPALIVE_URLS` at any http(s) URL, e.g. an
  uptime-ping service or your own domain.
- Toggle it live from the UI (**4. 24/7 keep-alive** panel) or via `POST /api/keepalive`;
  check `/healthz` or `GET /api/keepalive` for live ping stats.

Notes and honest limits:

- Sleep is opt-in on Railway: if Serverless/App Sleeping is off in the service settings,
  the service never sleeps and the pinger is simply harmless. If you enable it to save
  usage, the pinger keeps you awake anyway — pick one behavior.
- Keeping the container awake 24/7 consumes Railway usage around the clock; an idle
  paused service does not. Check Railway's pricing/metrics if that matters to you.
- The pinger survives nothing being configured: with no public domain and no
  `KEEPALIVE_URLS` it idles and says so in the log and UI instead of failing.
- Redeploys and process restarts still reset the in-memory loop (it is deliberately not
  persisted). With keep-alive on, the *process* no longer sleeps, so the loop keeps
  running until it succeeds, hits `MAX_ATTEMPTS`, or you stop it — raise `MAX_ATTEMPTS`
  (up to 100000) for long unattended hunts.
- Ping targets are redacted of query strings in logs/status responses so private
  ping-token URLs (e.g. healthchecks.io style UUIDs) are not quoted in full.

## Boot volume manager

Panel **5. Boot volume manager** manages the disks behind existing instances without touching the OCI Console:

- **Scan** lists every instance, boot volume (with what it is attached to), availability domain and boot volume backup, plus storage used against the 200 GB free-tier total.
- **Create** a new empty boot volume (50–32,768 GB) in a selected availability domain, or restore a fresh volume from an available backup. Empty volumes contain no operating system; restore from a backup when you need a bootable copy. The panel warns when the added storage would exceed the displayed free-tier allowance.
- **Stop / Start** the selected instance. OCI only lets a boot volume move while its instance is `STOPPED`.
- **Detach** releases the selected disk from its instance (instance must be stopped). The disk becomes `AVAILABLE` and keeps its data.
- **Attach / re-attach** puts an `AVAILABLE` disk onto a stopped instance that has no boot disk. Boot volumes never cross availability domains — the job refuses an AD mismatch instead of failing at OCI.
- **Delete** permanently removes a detached disk and reclaims its storage from the 200 GB quota.
- **Replace** swaps an instance's boot disk in one job: it stops the instance if needed, detaches the old disk, attaches the replacement (an existing detached disk, or a fresh volume restored from a backup), and boots the instance again. If the attach step fails, it rolls back: the original disk is re-attached and the instance started. Optionally it deletes the old disk after a successful boot.

Every mutating action runs as one background job (like the provisioning loop): the request returns immediately, progress streams into **Live output** and the Telegram live log when enabled, and a replace result can be sent as a Telegram alert. Only one boot-disk job runs at a time; the panel shows its state, offers a **Stop running boot disk job** button that aborts it at the next state poll without issuing further OCI changes, and re-scans the inventory automatically when the job ends.

## Run on a small VPS

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install --no-cache-dir -r requirements.txt
export APP_PASSWORD='choose-a-long-password'
gunicorn -c gunicorn.conf.py app:app
```

For a systemd or reverse-proxy setup, point the proxy at `127.0.0.1:5000`. The application itself binds to `0.0.0.0` when run by Gunicorn so the same command works in a container. Put TLS in Railway or in the VPS reverse proxy; do not expose an unauthenticated instance to the public internet.

## Environment variables

| Variable | Default | Description |
| --- | ---: | --- |
| `APP_PASSWORD` | empty | Basic Auth password. Set this in production. |
| `PORT` | `5000` | Supplied by Railway or used locally. |
| `MAX_ATTEMPTS` | `100` | Hard cap for one provisioning loop; bounded to 1–100000. The high ceiling supports genuine 24/7 hunting (~69 days at a 60s delay); the default stays conservative. |
| `KEEPALIVE_ENABLED` | `true` | Send periodic outbound pings so Railway's Serverless/App-Sleeping never pauses the service. Set `false` to disable. |
| `KEEPALIVE_URLS` | auto | Comma-separated http(s) URLs to ping. Auto-defaults to this app's own `https://$RAILWAY_PUBLIC_DOMAIN/healthz`, so on Railway it usually needs nothing. |
| `KEEPALIVE_INTERVAL_SECONDS` | `240` | Seconds between pings; bounded to 30–295 so it always beats Railway's ≥5-minute idle window. |
| `KEEPALIVE_TIMEOUT_SECONDS` | `10` | Per-ping HTTP timeout; bounded to 2–30. |
| `OCI_API_SLOTS` | `2` | Maximum concurrent OCI jobs/requests in the process; bounded to 1–8. |
| `USAGE_CACHE_SECONDS` | `20` | Short in-memory cache for the quota screen. Set to `0` to disable. No private key is cached. |
| `MAX_CONTENT_LENGTH` | `65536` | Maximum JSON request body in bytes. |
| `LOG_LEVEL` | `info` | Gunicorn log level. |

The provisioning loop is intentionally in memory. A Railway restart, redeploy, or VPS process restart stops it; start it again from the UI.
With the 24/7 keep-alive enabled (see below), the host no longer pauses the service, so a running loop keeps going around the clock. This avoids a database/queue dependency and keeps the service lightweight.

## Features

- Dark, dependency-free UI served from the Flask template
- OCI config parsing and private-key upload without server-side credential storage
- Ubuntu image and subnet discovery
- Free-tier storage, Micro, and Ampere A1 usage checks
- Boot volume manager: create empty or backup-restored boot volumes, attach, detach, re-attach, replace and delete boot disks, plus instance stop/start — all as one logged background job with rollback on a failed replace
- Bounded retry loop with fixed or randomized delays and availability-domain rotation
- Launch pre-flight check: before the first retry the loop resolves the image, subnet, ADs and shape against the configured region, so a guaranteed-fatal config is reported with an exact cause instead of silently burning attempts on an ambiguous OCI `404 NotAuthorizedOrNotFound` (e.g. a shape that is not offered in the selected region, or an image/subnet OCID copied from a different region)
- Telegram attempt updates with attempt number, OCI email, region/AD, and safe key fingerprint (never the private key)
- Optional Telegram success/failure alerts and throttled live logs
- Firewall/security-list and NSG inspection helpers
- `/healthz` health check for Railway, Docker and systemd
- 24/7 keep-alive pinger that stops Railway's Serverless/App-Sleeping from pausing the service

## API endpoints

| Endpoint | Method | Description |
| --- | --- | --- |
| `/healthz` | GET | Cheap unauthenticated health check |
| `/` | GET | Main UI |
| `/api/list-images` | POST | List available OS images |
| `/api/list-subnets` | POST | List available subnets |
| `/api/free-tier-status` | POST | Check quota usage |
| `/api/test-launch` | POST | Validate launch inputs without creating an instance |
| `/api/auto-launch-loop` | POST | Start the bounded provisioning loop |
| `/api/stop-loop` | POST | Stop the loop |
| `/api/logs` | GET | Fetch bounded live logs |
| `/api/status` | GET | Check loop status |
| `/api/keepalive` | GET/POST | Inspect or toggle the 24/7 keep-alive pinger |
| `/api/boot-volumes/list` | POST | Inventory instances, boot volumes, backups and availability domains with attachment state and storage totals |
| `/api/boot-volumes/action` | POST | Queue a boot disk job: `create` (empty or from `backup_id`), `detach`, `attach` (also re-attach), `delete`, `instance-action` (start/stop) or `replace` |
| `/api/boot-volumes/status` | GET | Boot disk job state (also included in `/api/status`) |
| `/api/boot-volumes/stop` | POST | Ask the running boot disk job to abort at its next state poll |
| `/api/scan-security-rules` | POST | Inspect NSG/security-list rules |
| `/api/open-firewall` | POST | Add firewall rules |
| `/api/test-telegram` | POST | Test Telegram credentials |
| `/api/send-telegram` | POST | Send a Telegram message |

## Resource choices

- No Redis, database, task queue, frontend build, or monitoring agent is required.
- The Docker image uses `python:3.12-slim-bookworm`, published Python wheels, a non-root user, and no runtime compiler toolchain.
- Gunicorn access logging is disabled because the browser polls status/log endpoints; application errors still go to stdout/stderr.
- The browser polls logs every five seconds only while provisioning and checks status every ten seconds, reducing idle requests.

## Security notes

- Set `APP_PASSWORD` and terminate HTTPS at Railway or a reverse proxy.
- OCI private keys and Telegram tokens are submitted for API calls but are not persisted to disk or included in the quota cache.
- The UI is intended for one operator. Do not run multiple Gunicorn workers or replicas unless process state is moved to a shared store.
