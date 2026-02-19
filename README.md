# livepeer-gateway

Python SDK for [Livepeer's](https://livepeer.org/) decentralized AI video processing network. Supports real-time **Live Video-to-Video** inference via orchestrator discovery (gRPC), HTTP trickle streaming, PyAV encoding/decoding, and on-chain/off-chain payments.

## Installation

```bash
pip install livepeer-gateway
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

For examples that use OpenCV/NumPy:

```bash
uv sync --group examples
```

## Architecture

```
                         livepeer-gateway SDK
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │  User Code                                              │
  │    │                                                    │
  │    ▼                                                    │
  │  start_lv2v(orch_url, req)                              │
  │    │                                                    │
  │    ├──► Orchestrator Discovery                          │
  │    │      SelectOrchestrator / DiscoverOrchestrators     │
  │    │      ┌─────────────────────────────────────┐       │
  │    │      │ 1. Explicit orch_url list            │       │
  │    │      │ 2. discovery_url endpoint            │       │
  │    │      │ 3. Signer discovery fallback          │       │
  │    │      │ → Parallel gRPC probes (up to 5)     │       │
  │    │      │ → First successful responder wins     │       │
  │    │      └─────────────────────────────────────┘       │
  │    │                                                    │
  │    ├──► PaymentSession                                  │
  │    │      Remote signer → initial payment headers       │
  │    │                                                    │
  │    ├──► POST /live-video-to-video                       │
  │    │      → LiveVideoToVideo job (publish/subscribe)    │
  │    │                                                    │
  │    ▼                                                    │
  │  LiveVideoToVideo (job)                                 │
  │    ├── .start_media(config) → MediaPublish              │
  │    │     └── .write_frame(av.VideoFrame)                │
  │    │           Frame → H.264 encode → MPEG-TS segments  │
  │    │           → TricklePublisher → Orchestrator        │
  │    │                                                    │
  │    ├── .media_output() → MediaOutput                    │
  │    │     └── .frames() → async iterator                 │
  │    │           TrickleSubscriber → MPEG-TS decode        │
  │    │           → av.VideoFrame                          │
  │    │                                                    │
  │    ├── .control → Control                               │
  │    │     └── .write_control(dict) → JSON messages       │
  │    │                                                    │
  │    ├── .events → Events                                 │
  │    │     └── events() → async iterator of dicts         │
  │    │                                                    │
  │    └── .close() → cleanup all resources                 │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
                          │           ▲
                          │           │
              H.264/MPEG-TS           │  AI-processed
              via trickle             │  MPEG-TS segments
                          ▼           │
                ┌─────────────────────────┐
                │   Livepeer Network      │
                │   (Orchestrator Node)   │
                │                         │
                │   AI Inference Pipeline │
                └─────────────────────────┘
```

## Key Workflows

### Job Lifecycle

```
  User Code                          SDK                           Orchestrator
  ─────────                          ───                           ────────────
      │                               │                                │
      │  start_lv2v(orch, req)        │                                │
      │──────────────────────────────►│                                │
      │                               │  gRPC GetOrchInfo              │
      │                               │───────────────────────────────►│
      │                               │◄───────────────────────────────│
      │                               │  OrchestratorInfo              │
      │                               │                                │
      │                               │  PaymentSession.get_payment()  │
      │                               │──► Remote Signer               │
      │                               │◄──                             │
      │                               │                                │
      │                               │  POST /live-video-to-video     │
      │                               │───────────────────────────────►│
      │                               │◄───────────────────────────────│
      │                               │  {publish_url, subscribe_url}  │
      │                               │                                │
      │  job (LiveVideoToVideo)       │                                │
      │◄──────────────────────────────│                                │
      │                               │                                │
      │  media = job.start_media()    │                                │
      │  await media.write_frame(f)   │  ───► trickle publish ────────►│
      │                               │                                │
      │  output = job.media_output()  │                                │
      │  async for f in output.frames │  ◄─── trickle subscribe ◄─────│
      │                               │                                │
      │  await job.close()            │  close control, media, payment │
      │──────────────────────────────►│───────────────────────────────►│
```

### Media Pipeline

```
  Input Frame (av.VideoFrame)
       │
       ▼
  MediaPublish.write_frame()
       │
       ├── Reformat to yuv420p
       ├── Compute PTS (source or wallclock)
       ├── Set keyframe on interval
       ▼
  libx264 H.264 Encoder
       │
       ▼
  MPEG-TS Segment (via PyAV segment muxer)
       │
       ▼
  TricklePublisher ──HTTP POST──► Orchestrator
                                       │
                                  AI Inference
                                       │
  TrickleSubscriber ◄──HTTP GET───────┘
       │
       ▼
  MPEG-TS Decoder (MpegTsDecoder)
       │
       ▼
  Output Frame (av.VideoFrame)
```

### Payment Flow

```
  PaymentSession                Remote Signer              Orchestrator
  ──────────────                ─────────────              ────────────
       │                              │                          │
       │  POST /sign-orchestrator-info│                          │
       │─────────────────────────────►│                          │
       │  {address, signature}        │                          │
       │◄─────────────────────────────│                          │
       │                              │                          │
       │  POST /generate-live-payment │                          │
       │─────────────────────────────►│                          │
       │  {payment, segCreds, state}  │                          │
       │◄─────────────────────────────│                          │
       │                              │                          │
       │  (Background) per-segment payment sender                │
       │  TrickleSubscriber monitors output segments             │
       │  Every ~5s: get_payment() → POST /payment ─────────────►│
       │                              │                          │
       │  On HTTP 480: refresh OrchestratorInfo and retry        │
       │  On HTTP 482: skip this payment cycle                   │
```

### Orchestrator Discovery

```
  Discovery Precedence (highest → lowest):

  1. Explicit orch_url ──────────► Use directly
                                    │ (empty/None falls through)
  2. discovery_url ──────────────► GET discovery endpoint
                                    │ (returns [{address: "..."}])
  3. signer_url ─────────────────► GET {signer}/discover-orchestrators
                                    │ (returns [{address: "..."}])
                                    ▼
  SelectOrchestrator:
    Take up to 5 candidates
    ├── ThreadPoolExecutor (parallel)
    │   ├── gRPC GetOrchInfo(candidate_1)
    │   ├── gRPC GetOrchInfo(candidate_2)
    │   └── ...
    └── First successful response wins
```

## API Reference

### Core

| Export | Type | Description |
|---|---|---|
| `start_lv2v(orch_url, req, ...)` | Function | Start a Live Video-to-Video job (sync) |
| `LiveVideoToVideo` | Dataclass | Job handle with publish/subscribe URLs, control, events |
| `StartJobRequest` | Dataclass | Job parameters: `model_id`, `params`, `request_id`, `stream_id` |

### Media

| Export | Type | Description |
|---|---|---|
| `MediaPublish` | Class | Encodes and publishes `av.VideoFrame`s via trickle streaming |
| `MediaPublishConfig` | Dataclass | Config: `fps`, `mime_type`, `keyframe_interval_s` |
| `MediaOutput` | Class | Subscribes to trickle output; yields segments, bytes, or frames |
| `VideoDecodedMediaFrame` | Dataclass | Decoded video frame with `frame`, `pts_time`, `kind="video"` |
| `AudioDecodedMediaFrame` | Dataclass | Decoded audio frame with `frame`, `pts_time`, `kind="audio"` |

### Discovery & Orchestration

| Export | Type | Description |
|---|---|---|
| `SelectOrchestrator(orch, ...)` | Function | Select best orchestrator via parallel gRPC probes |
| `DiscoverOrchestrators(orch, ...)` | Function | Discover orchestrator addresses |
| `get_orch_info(url, ...)` | Function | Get `OrchestratorInfo` from a single node via gRPC |
| `CapabilityId` | IntEnum | Capability identifiers (e.g., `LIVE_VIDEO_TO_VIDEO = 35`) |
| `build_capabilities(cap, model)` | Function | Build a `Capabilities` protobuf message |

### Payments & Errors

| Export | Type | Description |
|---|---|---|
| `PaymentSession` | Class | Manages payment generation via remote signer |
| `LivepeerGatewayError` | Exception | Base error for the library |
| `NoOrchestratorAvailableError` | Exception | No orchestrator could be selected |
| `PaymentError` | Exception | Payment operation failed |

### Transport

| Export | Type | Description |
|---|---|---|
| `TricklePublisher` | Class | HTTP trickle segment publisher |
| `TrickleSubscriber` | Class | HTTP trickle segment subscriber |
| `SegmentReader` | Class | Read bytes from a trickle segment |
| `Control` | Class | Publish JSON control messages via trickle |
| `Events` | Class | Subscribe to JSON event messages via trickle |

## Token Authentication

Jobs can be configured via a base64-encoded JSON token:

```python
import base64, json
from livepeer_gateway.lv2v import StartJobRequest, start_lv2v

payload = {
    "signer": "https://signer.example.com",
    "signer_headers": {"Authorization": "Bearer abcdef"},
    "discovery": "https://discovery.example.com",
    "discovery_headers": {"Authorization": "Bearer qwerty"},
}
token = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

job = start_lv2v(
    orch_url=None,
    req=StartJobRequest(model_id="noop"),
    token=token,
)
```

### Token Schema

| Field | Type | Description |
|---|---|---|
| `signer` | `string` (optional) | Signer base URL |
| `signer_headers` | `{string: string}` (optional) | HTTP headers sent to all signer endpoints |
| `discovery` | `string` (optional) | Discovery endpoint URL |
| `discovery_headers` | `{string: string}` (optional) | HTTP headers sent to the discovery endpoint |

Explicit keyword arguments always take precedence over token values. `signer_headers` are sent with requests to the signer service. `discovery_headers` are only used when an explicit `discovery_url` is provided.

## Examples

| Example | Description | Command |
|---|---|---|
| `get_orchestrator_info.py` | Query orchestrator info via gRPC | `uv run examples/get_orchestrator_info.py localhost:8935` |
| `select_orchestrator.py` | Select best orchestrator from candidates | `uv run examples/select_orchestrator.py` |
| `start_job.py` | Start a LV2V job | `uv run examples/start_job.py localhost:8935` |
| `write_frames.py` | Write raw frames to a job | `uv run examples/write_frames.py localhost:8935` |
| `camera_capture.py` | Capture MacOS camera and publish frames | `uv run examples/camera_capture.py localhost:8935` |
| `in_out_composite.py` | Side-by-side input/output with latency overlay | `uv sync --group examples && uv run examples/in_out_composite.py localhost:8935` |
| `subscribe_events.py` | Subscribe to job events | `uv run examples/subscribe_events.py localhost:8935` |
| `write_control.py` | Send control messages to a job | `uv run examples/write_control.py localhost:8935` |
| `payments.py` | Payment flow demonstration | `uv run examples/payments.py` |

### Camera Capture with Output

```bash
# View output in ffplay
uv run examples/camera_capture.py localhost:8935 --output - | \
  ffplay -fflags nobuffer -flags low_delay -probesize 32 -i -

# Save output to file
uv run examples/camera_capture.py localhost:8935 --output out.ts
```

### On-Chain Mode

```bash
uv run examples/get_orchestrator_info.py --signer-url "<signer-host:port>"
```

## Protobuf Generation

```bash
uv run generate-lp-rpc
```

## Web Wrapper & Docker

A FastAPI web wrapper and Docker setup are provided for serving the SDK over HTTP/WebSocket:

```bash
# Build and run with Docker Compose
docker compose up --build

# Open browser at http://localhost:8000
```

See [docs/serverless-analysis.md](docs/serverless-analysis.md) for hosting analysis and deployment options.

## Requirements

- Python >= 3.10
- FFmpeg libraries (libavcodec, libavformat, etc.)
- Dependencies: `grpcio`, `aiohttp`, `av`
