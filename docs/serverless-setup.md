# Livepeer Gateway — Serverless Setup Guide

## Current Status

The gateway API is deployed on Cloud Run and running:

```
https://livepeer-gateway-90265565772.us-central1.run.app
```

| What | Status |
|------|--------|
| Cloud Run service | Deployed, healthy |
| `GET /health` | Working |
| `GET /jobs` | Working |
| `POST /start-job` | Blocked — needs Daydream credentials |
| WebSocket streaming | Blocked — needs a running job |
| Control / Events | Blocked — needs a running job |

## What's Missing

One environment variable: **`LIVEPEER_TOKEN`**

The orchestrator requires signed requests. Daydream (`https://daydream.live`) handles signing, discovery, and payments. The SDK already speaks Daydream's protocol — it just needs credentials.

## Setup Steps

### 1. Get a Daydream API Key

Sign up or log in at [https://daydream.live](https://daydream.live) and obtain your API key.

### 2. Encode the Token

The `LIVEPEER_TOKEN` is a base64-encoded JSON object:

```python
import base64, json

token = base64.b64encode(json.dumps({
    "signer": "https://daydream.live",
    "signer_headers": {
        "Authorization": "Bearer <YOUR_DAYDREAM_API_KEY>"
    },
    "discovery": "https://daydream.live",
    "discovery_headers": {
        "Authorization": "Bearer <YOUR_DAYDREAM_API_KEY>"
    }
}).encode()).decode()

print(token)
```

Or from the command line:

```bash
echo -n '{"signer":"https://daydream.live","signer_headers":{"Authorization":"Bearer <YOUR_DAYDREAM_API_KEY>"},"discovery":"https://daydream.live","discovery_headers":{"Authorization":"Bearer <YOUR_DAYDREAM_API_KEY>"}}' | base64
```

### 3. Update Cloud Run

```bash
gcloud run services update livepeer-gateway \
    --project naap-sdk \
    --region us-central1 \
    --set-env-vars "LIVEPEER_TOKEN=<base64_token>,DAYDREAM_URL=https://daydream.live"
```

### 4. Verify

```bash
# Health check
curl https://livepeer-gateway-90265565772.us-central1.run.app/health

# Start a job (should succeed now)
curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"model_id": "noop"}' \
  https://livepeer-gateway-90265565772.us-central1.run.app/start-job
```

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LIVEPEER_TOKEN` | **Yes** | — | Base64 JSON with Daydream signer/discovery URLs and auth headers |
| `DAYDREAM_URL` | Recommended | — | `https://daydream.live` — overrides `SIGNER_URL` as a shortcut |
| `ORCHESTRATOR_URL` | No | — | Direct orchestrator `host:port`. If omitted, Daydream discovery picks one automatically |
| `API_KEYS` | No | — | Comma-separated API keys to enable auth (e.g. `sk-abc,sk-xyz`). Empty = open access |
| `MAX_JOBS_PER_KEY` | No | `10` | Max concurrent jobs per API key |
| `DEFAULT_MODEL_ID` | No | `noop` | Default model when not specified in request |
| `FPS` | No | `24` | Frame publishing rate |
| `JPEG_QUALITY` | No | `80` | Output JPEG quality (0-100) |

## API Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/` | No | Browser app |
| `GET` | `/health` | No | Health check |
| `GET` | `/jobs` | Yes* | List active jobs |
| `POST` | `/start-job` | Yes* | Start AI inference job |
| `GET` | `/job/{id}` | Yes* | Job status |
| `DELETE` | `/stop-job/{id}` | Yes* | Stop job |
| `POST` | `/job/{id}/control` | Yes* | Send control message |
| `GET` | `/job/{id}/events` | Yes* | SSE event stream |
| `WS` | `/ws/stream?job_id=X` | Yes* | Bidirectional JPEG streaming |

*Auth required only when `API_KEYS` is set. Currently disabled (open access).
