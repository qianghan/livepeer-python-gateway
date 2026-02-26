# Daydream Integration

Daydream (`https://daydream.live`) is a managed billing and signing backend for the Livepeer network. It handles orchestrator discovery, payment signing, and ticket management so you don't need to run your own Ethereum node or manage on-chain payments directly.

## How It Works

The livepeer-gateway SDK communicates with a **remote signer** to:

1. **Discover orchestrators** — `POST /discover-orchestrators`
2. **Sign orchestrator info** — `POST /sign-orchestrator-info`
3. **Generate payments** — `POST /generate-live-payment`

Daydream implements this exact HTTP contract. No SDK code changes are needed — you just point `DAYDREAM_URL` at Daydream and provide credentials via `LIVEPEER_TOKEN`.

## Quick Start

### 1. Get Daydream Credentials

Sign up at [https://daydream.live](https://daydream.live) to obtain your API credentials.

### 2. Encode Your Token

The `LIVEPEER_TOKEN` is a base64-encoded JSON object containing your signer URL and auth headers:

```python
import base64, json

token = base64.b64encode(json.dumps({
    "signer": "https://daydream.live",
    "signer_headers": {
        "Authorization": "Bearer YOUR_DAYDREAM_API_KEY"
    },
    "discovery": "https://daydream.live",
    "discovery_headers": {
        "Authorization": "Bearer YOUR_DAYDREAM_API_KEY"
    }
}).encode()).decode()

print(token)
```

Or from the command line:

```bash
echo -n '{"signer":"https://daydream.live","signer_headers":{"Authorization":"Bearer YOUR_DAYDREAM_API_KEY"},"discovery":"https://daydream.live","discovery_headers":{"Authorization":"Bearer YOUR_DAYDREAM_API_KEY"}}' | base64
```

### 3. Configure the Gateway

Set the following environment variables:

```bash
# .env
DAYDREAM_URL=https://daydream.live
LIVEPEER_TOKEN=<base64 token from step 2>
```

`DAYDREAM_URL` is a convenience shortcut — when set, it overrides `SIGNER_URL` so you don't need to set both.

### 4. Run

```bash
docker compose up --build
```

The gateway will use Daydream for orchestrator discovery and payment signing automatically.

## Configuration Priority

The gateway resolves the signer URL in this order:

1. **`DAYDREAM_URL`** (highest priority)
2. **`SIGNER_URL`**
3. **`signer` field inside `LIVEPEER_TOKEN`**

If `DAYDREAM_URL` is set, it takes precedence over `SIGNER_URL`. The `LIVEPEER_TOKEN` provides auth headers regardless of which URL is used.

## Cost Implications

When using Daydream with a funded account, real payments are sent to orchestrators for AI processing. Costs depend on:

- **Model complexity** — more compute-intensive models cost more per frame
- **Frame rate and resolution** — higher FPS / resolution = more segments = more payments
- **Session duration** — payments are sent continuously while a job is active

Monitor your Daydream dashboard for usage and billing details. For testing without real payments, omit `DAYDREAM_URL` and `SIGNER_URL` to run in **offchain mode** (no payments sent).

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `401 Unauthorized` from signer | Invalid or expired Daydream API key | Re-generate token with valid credentials |
| `No orchestrator available` | Discovery returned no results | Check Daydream dashboard; ensure your account is active |
| Job starts but no output | Orchestrator rejected payment | Verify your Daydream account is funded |
| `PaymentError: refresh failed` | Signer state desync | Restart the job — the SDK retries automatically |
