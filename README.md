# OCI Provisioner Portal v2

A modern, deployable web interface for automating OCI Always Free tier instance provisioning with Telegram alerts.

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template)

### Manual Deploy

1. **Fork/clone this repo**
2. **Create a new Railway project** and connect your repo
3. **Set environment variables** in Railway dashboard:
   - `APP_PASSWORD` — Optional HTTP Basic Auth password (leave empty for no auth)
   - `PORT` — Set automatically by Railway (default: 5000)
4. **Deploy**

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_PASSWORD` | No | HTTP Basic Auth password. Leave empty to disable auth. |
| `PORT` | No | Server port. Railway sets this automatically. |
| `MAX_ATTEMPTS` | No | Max provisioning attempts (default: 100) |

## Features

- **Dark theme UI** — Clean, modern interface with collapsible panels
- **Auto config parsing** — Paste raw OCI config, fields auto-extract
- **Free tier quota check** — Visual progress bars for storage, micro instances, ARM OCPUs
- **All-OS mode** — Switch between Ubuntu-only or all operating systems
- **Dynamic retry delays** — Fixed or randomized 25-60s to avoid rate limits
- **Telegram alerts** — Get notified on success, failure, or errors
- **Live terminal** — Real-time log streaming with color-coded severity
- **Phnom Penh timezone** — All timestamps in ICT (UTC+7)

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main UI |
| `/api/list-images` | POST | List available OS images |
| `/api/free-tier-status` | POST | Check quota usage |
| `/api/auto-launch-loop` | POST | Start provisioning loop |
| `/api/stop-loop` | POST | Stop provisioning loop |
| `/api/logs` | GET | Fetch live logs (offset-based) |
| `/api/status` | GET | Check automation status |
| `/api/test-telegram` | POST | Test Telegram connection |
| `/api/send-telegram` | POST | Send custom Telegram message |

## File Structure

```
oci-provisioner-v2/
├── app.py                  # Flask backend
├── requirements.txt        # Python dependencies
├── Procfile               # Railway process definition
├── runtime.txt            # Python version
├── railway.toml           # Railway config
├── nixpacks.toml          # Nixpacks build config
└── templates/
    └── index.html         # Frontend UI
```

## Local Development

```bash
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`

## Security Notes

- Set `APP_PASSWORD` for production use
- All API endpoints require Basic Auth when `APP_PASSWORD` is set
- Security headers included (HSTS, XSS protection, frame options)
- Credentials are never logged or stored server-side
