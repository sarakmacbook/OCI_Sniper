# OCI Provisioner Portal

A small Flask web UI for launching OCI Always Free instances with optional Telegram alerts. It is designed to run as **one Gunicorn worker** on Railway or a low-spec VPS.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new?utm_medium=integration&utm_source=button&utm_campaign=generic)

## Deploy on Railway

1. Create a Railway service from this repository.
2. Set `APP_PASSWORD` in the service variables. This enables HTTP Basic Auth for the UI and API.
3. Deploy. Railway supplies `PORT`; the included Dockerfile and `railway.toml` use it automatically.
4. Use `/healthz` as the health endpoint if you configure Railway manually.

The container uses one worker and two threads. This is deliberate: the provisioning loop is process-local, and multiple workers would create separate status/log stores and could make the UI misleading. The OCI SDK is imported only when an OCI API operation is first used.

There is **exactly one provisioning-loop slot per service**. A second start request is refused while a loop is running in the background — even when it arrives from another browser with a *different* OCI key, region or shape — and the refusal is written to the live log so the reason is visible in the UI terminal and on Telegram. See [One loop at a time](#one-loop-at-a-time).

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

## One loop at a time

The provisioning loop runs in one background thread with one owner: the service.
Every start request has to reserve that single slot before any OCI client is
created, and the slot is released only when the loop actually exits (success,
retry limit, stop request, or a fatal setup/pre-flight error).

What that means in practice:

- Starting a second loop while one is running is refused, no matter what the
  request carries. A different OCI key, tenancy, region or shape does not open a
  second loop — OCI keys are not loop owners, the service is.
- Each refusal is answered with the loop that already owns the slot
  (`run #2: shape VM.Standard.A1.Flex · region ap-kulai-1 · key aa:bb`) **and**
  written to the live log, so the UI terminal and the Telegram live log both say
  what is running and why nothing new started.
- A refused request cannot change the running loop's settings. (Before, a second
  start with "live log" unticked silently switched off the running loop's
  Telegram live log.)
- **Stop** is confirmed in the live log twice: `Stop requested for run #N …`
  when the button is pressed, then `Provisioning loop exited (stopped by user).`
  once the in-flight OCI call returns and the thread leaves the loop. The exit
  line still reaches Telegram; the live log is switched off right after it.
- Every exit is recorded with a reason (`success — instance created on attempt 3`,
  `stopped by user`, `retry limit reached (100 attempts)`, `pre-flight check failed`,
  …) and is visible in `/api/status` under `loop.stop_reason`.
- Each run gets a fresh stop event and a run id. A slow/stale thread can never
  clear the state of a newer run, and a Stop click can only ever affect the loop
  that is running now.
- `/api/status` reports the loop that owns the slot — `run_id`, `shape`,
  `region`, `fingerprint`, `attempts`, `current_ad`, `started_at_local`,
  `finished_at_local`, `stop_reason` — never the private key. The header badge in
  the UI shows the same summary while a loop is hunting.

This is still process-local: running more than one Gunicorn worker or more than
one replica means more than one loop slot. Keep the deployment at one worker
(the shipped `gunicorn.conf.py` and Docker command do) unless process state is
moved to a shared store.

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

## Demo mode (try the UI without credentials)

```bash
DEMO_MODE=1 APP_PASSWORD='' PORT=8000 gunicorn -c gunicorn.conf.py app:app
```

Open `http://localhost:8000` and the form arrives prefilled with fake credentials. Everything the
loop guard does can be exercised in a minute: scan images → pick a subnet → **Start** (the demo cloud
is `OutOfHostCapacity` for two attempts, then succeeds) → start again from a second tab with a
different OCI key while it hunts and watch the refusal appear in the live log, header badge and
response → **Stop** and read the `Provisioning loop exited (stopped by user).` line.

The demo also mirrors Oracle's per-chipset images: scanning returns `aarch64` images for the Ampere
A1 shape and x86_64 images for the AMD E2 Micro shape, so switching Shape reproduces the real
"re-scan OS images for the new chipset" prompt (and refusing to start with a stale pairing) without
touching OCI.

Demo mode is honest about its limits: every log line is prefixed `[demo]`, a banner is shown in the
UI, `/healthz` reports `"demo_mode": true`, read-only panels (images, subnets, quota, boot volume
inventory, firewall scan) return the in-memory state, and write panels that are not simulated
(firewall changes, boot-volume jobs) fail with `501 Demo mode: … is not simulated` instead of
pretending to have changed a real account. Without `DEMO_MODE`, `demo_sdk.py` is never imported.

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
| `DEMO_MODE` | `false` | Serve the UI against an in-memory OCI double (`demo_sdk.py`): no credentials, no Oracle Cloud calls, no instance created, every log line prefixed `[demo]`. For previews and screenshots only — never enable in production. |
| `ALLOW_IFRAME_PREVIEW` | `false` | Drop the `X-Frame-Options: DENY` header so the UI can run inside a hosted preview iframe. Off by default: production keeps `DENY`. |
| `DEMO_CAPACITY_AFTER_ATTEMPTS` | `3` | Demo mode only: attempt number that finally succeeds; earlier attempts fail with `OutOfHostCapacity`. |

The provisioning loop is intentionally in memory. A Railway restart, redeploy, or VPS process restart stops it; start it again from the UI.
With the 24/7 keep-alive enabled (see below), the host no longer pauses the service, so a running loop keeps going around the clock. This avoids a database/queue dependency and keeps the service lightweight.

## Features

- Dark, dependency-free UI served from the Flask template
- OCI config parsing and private-key upload without server-side credential storage
- Ubuntu image and subnet discovery
- Free-tier storage, Micro, and Ampere A1 usage checks
- Boot volume manager: create empty or backup-restored boot volumes, attach, detach, re-attach, replace and delete boot disks, plus instance stop/start — all as one logged background job with rollback on a failed replace
- Bounded retry loop with fixed or randomized delays and availability-domain rotation
- Optional demo mode (`DEMO_MODE=1`) that runs the whole UI against an in-memory OCI double for previews and screenshots
- Single-loop guarantee: one provisioning loop per service, refused start requests logged live (UI + Telegram) with the OCI key/region that already holds the slot, and an exit line with a reason for every stop
- Launch pre-flight check: before the first attempt the loop resolves the image, subnet and ADs against the configured region, so a truly fatal config (e.g. an image/subnet OCID copied from a different region) is reported with an exact cause instead of silently burning attempts on an ambiguous OCI `404 NotAuthorizedOrNotFound`
- Chipset-aware image scanning: Ampere A1/A2 shapes are ARM (`aarch64`) and the standard E-series/Micro shapes are AMD/Intel (`x86_64`), and OCI image OCIDs are built for exactly one of the two. Switching the shape across chipsets clears the scanned image list and asks for a fresh scan (`/api/list-images` filters by the shape's chipset and echoes `shape`/`arch` back), starting is blocked while the image and shape disagree, and the pre-flight check rejects a mismatched pairing with the image and shape chipsets named instead of letting it 404 forever
- Shape-availability gate: if Oracle has not yet offered the selected shape in the region, the loop logs it live and **waits** — re-checking each attempt and launching automatically the moment the shape appears — instead of exiting or hammering a guaranteed 404
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
| `/api/auto-launch-loop` | POST | Start the bounded provisioning loop; refused (with a live-log line) while another loop owns the single slot |
| `/api/stop-loop` | POST | Ask the one running loop to exit; logs `Stop requested …` and returns `running` so the UI can wait for the exit line |
| `/api/logs` | GET | Fetch bounded live logs |
| `/api/status` | GET | Check loop status; `loop` carries run id, shape, region, key fingerprint, attempts, AD and exit reason |
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
