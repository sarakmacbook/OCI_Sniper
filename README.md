# OCI Provisioner Portal

A small Flask web UI for launching OCI Always Free instances with optional Telegram alerts. It is designed to run as **one Gunicorn worker** on Railway or a low-spec VPS.

## Deploy on Railway

1. Create a Railway service from this repository.
2. Set `APP_PASSWORD` in the service variables. This enables HTTP Basic Auth for the UI and API.
3. Deploy. Railway supplies `PORT`; the included Dockerfile and `railway.toml` use it automatically.
4. Use `/healthz` as the health endpoint if you configure Railway manually.

The container uses one worker and two threads. This is deliberate: the provisioning loop is process-local, and multiple workers would create separate status/log stores and could make the UI misleading. The OCI SDK is imported only when an OCI API operation is first used.

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
| `MAX_ATTEMPTS` | `100` | Hard cap for one provisioning loop; bounded to 1–1000. Prevents an orphaned daemon retrying forever. |
| `OCI_API_SLOTS` | `2` | Maximum concurrent OCI jobs/requests in the process; bounded to 1–8. |
| `USAGE_CACHE_SECONDS` | `20` | Short in-memory cache for the quota screen. Set to `0` to disable. No private key is cached. |
| `MAX_CONTENT_LENGTH` | `65536` | Maximum JSON request body in bytes. |
| `LOG_LEVEL` | `info` | Gunicorn log level. |

The provisioning loop is intentionally in memory. A Railway restart, redeploy, sleep, or VPS process restart stops it; start it again from the UI. This avoids a database/queue dependency and keeps the service lightweight.

## Features

- Dark, dependency-free UI served from the Flask template
- OCI config parsing and private-key upload without server-side credential storage
- Ubuntu image and subnet discovery
- Free-tier storage, Micro, and Ampere A1 usage checks
- Bounded retry loop with fixed or randomized delays and availability-domain rotation
- Telegram attempt updates with attempt number, OCI email, region/AD, and safe key fingerprint (never the private key)
- Optional Telegram success/failure alerts and throttled live logs
- Firewall/security-list and NSG inspection helpers
- `/healthz` health check for Railway, Docker and systemd

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
| `/api/list-vnics` | POST | List VNICs |
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
