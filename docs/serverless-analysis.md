# Serverless / Hosted SDK Analysis

How to host the `livepeer-gateway` Python SDK as a web service so that browser applications can consume Livepeer's AI video processing without any native dependencies.

## Architecture

```
  Browser                    Hosted SDK (Cloud Run)              Livepeer Network
  ───────                    ──────────────────────              ────────────────
     │                              │                                  │
     │  POST /start-job             │                                  │
     │  {model_id, orch_url}        │                                  │
     │─────────────────────────────►│                                  │
     │                              │  start_lv2v()                    │
     │                              │  gRPC + HTTP ───────────────────►│
     │                              │◄────────────────────────────────│
     │  {job_id}                    │                                  │
     │◄─────────────────────────────│                                  │
     │                              │                                  │
     │  WebSocket /ws/stream        │                                  │
     │  ?job_id=X                   │                                  │
     │◄════════════════════════════►│                                  │
     │                              │                                  │
     │  Send: JPEG frame (binary)   │                                  │
     │════════════════════════════► │  decode JPEG → av.VideoFrame     │
     │                              │  media.write_frame() ──────────►│
     │                              │                                  │
     │                              │  AI inference                    │
     │                              │                                  │
     │                              │  media_output.frames() ◄────────│
     │  Recv: JPEG frame (binary)   │  av.VideoFrame → encode JPEG    │
     │◄════════════════════════════ │                                  │
     │                              │                                  │
     │  DELETE /stop-job/X          │                                  │
     │─────────────────────────────►│  job.close()                     │
     │                              │──────────────────────────────────►│
```

## Platform Comparison

| Criteria | Cloud Run | AWS Lambda | AWS Fargate |
|---|---|---|---|
| **WebSocket support** | Yes (up to 60 min) | No native support | Yes |
| **Container support** | Yes (Docker) | Yes (container images) | Yes (Docker) |
| **Max execution time** | 60 min | 15 min | Unlimited |
| **Package size limit** | ~32 GB (container) | 250 MB (10 GB container) | No limit |
| **PyAV + FFmpeg** | Works in container | Tight fit (layers needed) | Works in container |
| **Auto-scaling** | Yes (0 to N) | Yes (0 to N) | Yes (min 1 task) |
| **Scale to zero** | Yes | Yes | No (min 1 task) |
| **Cold start** | ~2-5s | ~1-3s | ~30-60s |
| **Cost model** | Per request + CPU-sec | Per invocation + duration | Per vCPU-hour |
| **Deployment** | `gcloud run deploy` | SAM/CDK/Terraform | ECS task definition |
| **Recommendation** | **Best fit** | Poor fit | Viable alternative |

### Cloud Run (Recommended)

Cloud Run is the best platform for hosting this SDK:

- **WebSocket support** allows bidirectional frame streaming for the full session duration (up to 60 minutes)
- **Container-based** deployment means PyAV, FFmpeg, and all native dependencies work without layer hacking
- **Scale to zero** keeps costs low when idle
- **Auto-scaling** handles burst traffic with per-request concurrency controls
- **gRPC support** means the SDK's orchestrator discovery works without modification

### AWS Lambda (Poor Fit)

Lambda is not well-suited for this workload:

- **No native WebSocket** support (requires API Gateway WebSocket API with connection management in DynamoDB, adding significant complexity)
- **250 MB deployment limit** is tight for PyAV + FFmpeg (though container images up to 10 GB work, cold starts increase)
- **15-minute max execution** limits session duration
- **Stateless model** conflicts with the SDK's long-lived job sessions

### AWS Fargate (Viable Alternative)

Fargate works but has operational overhead:

- **Always-on minimum** of 1 task means higher baseline cost
- **Slower scaling** with 30-60s cold starts for new tasks
- **ECS complexity** requires task definitions, services, load balancers
- **Good for:** steady-state workloads with predictable traffic

## Developer Experience Assessment

### Cloud Run

```bash
# Deploy
docker build -t livepeer-gateway .
docker tag livepeer-gateway gcr.io/PROJECT/livepeer-gateway
docker push gcr.io/PROJECT/livepeer-gateway
gcloud run deploy livepeer-gateway \
  --image gcr.io/PROJECT/livepeer-gateway \
  --allow-unauthenticated \
  --port 8000 \
  --timeout 3600 \
  --set-env-vars "ORCHESTRATOR_URL=host:port"
```

- Simple CLI deployment
- Built-in HTTPS + custom domain
- Automatic TLS termination
- Integrated logging and monitoring
- WebSocket "just works" with `--timeout` flag

### Lambda

```bash
# Requires: API Gateway WebSocket API + DynamoDB + Lambda function
# Connection management code needed for $connect/$disconnect/$default routes
# Frame relay requires separate Lambda invocations per message
# Cold starts with PyAV container images: 5-15s
```

- High setup complexity for WebSocket
- Additional infrastructure (API Gateway, DynamoDB)
- Per-message Lambda invocations add latency
- Container image cold starts are slow with PyAV

### Fargate

```bash
# Requires: ECS cluster, task definition, service, ALB, target group
# Manual scaling policies
# VPC configuration
# Service discovery setup
```

- More infrastructure to manage
- Good operational tooling (CloudWatch, X-Ray)
- Predictable performance once running

## How the Browser Consumes the Hosted SDK

### REST API for Job Lifecycle

```
POST   /start-job   → {job_id: "uuid"}     Start AI processing job
GET    /job/{id}     → {status, model_id}   Check job status
DELETE /stop-job/{id} → {status: "stopped"} Stop and cleanup
GET    /health       → {status: "ok"}       Health check
```

### WebSocket for Frame Streaming

```
WS /ws/stream?job_id={id}

Client → Server: Binary JPEG frames (camera capture via canvas.toBlob)
Server → Client: Binary JPEG frames (AI-processed output)

Protocol:
1. Browser sends JPEG at ~24 FPS (~20-40 KB per frame)
2. Server decodes JPEG → av.VideoFrame
3. Server feeds frame to SDK (H.264 encode → trickle publish)
4. Server reads AI output (trickle subscribe → H.264 decode)
5. Server encodes output frame as JPEG → sends to browser
```

### Browser-Side Frame Pipeline

```
  getUserMedia(640x480, 24fps)
       │
       ▼
  Hidden <canvas> drawImage()
       │
       ▼
  canvas.toBlob('image/jpeg', 0.8)
       │  ~20-40 KB per frame
       ▼
  WebSocket.send(blob)
       │
       ▼
  ─── network ───
       │
       ▼
  WebSocket.onmessage
       │
       ▼
  createImageBitmap(blob)
       │
       ▼
  Output <canvas> drawImage()
```

## Pros & Cons

| Pros | Cons |
|---|---|
| Browser gets AI video with zero native dependencies | Extra network hop adds latency (~10-50ms) |
| Scales horizontally with demand | JPEG over WebSocket uses more bandwidth than H.264 (~3-5x) |
| Payments and signing handled server-side | Cost per active session (compute + memory) |
| Works on any device with a modern browser | WebSocket state management adds complexity |
| Single deployment serves all clients | Server must hold job state in memory |
| HTTPS/WSS provides transport security | Session affinity needed for multi-instance |
| No CORS issues when serving browser app | Cold start delay on first request |

## Cost Estimation (Cloud Run)

Assuming 640x480 @ 24 FPS, JPEG quality 0.8 (~30 KB/frame):

| Metric | Value |
|---|---|
| Bandwidth per session | ~720 KB/s in + ~720 KB/s out = ~1.4 MB/s |
| CPU per session | ~0.5-1 vCPU (H.264 encode/decode + JPEG conversion) |
| Memory per session | ~256-512 MB |
| Cost per hour (Cloud Run) | ~$0.05-0.10 per session |
| Idle cost | $0 (scale to zero) |

## Deployment Checklist

1. Build Docker image with `Dockerfile` in repo root
2. Push to container registry (GCR, ECR, Docker Hub)
3. Deploy to Cloud Run with WebSocket timeout set to max session duration
4. Set environment variables: `ORCHESTRATOR_URL`, optionally `SIGNER_URL`, `LIVEPEER_TOKEN`
5. Configure CORS if browser app is served from a different domain
6. Set up health check on `/health` endpoint
7. Configure autoscaling: min 0, max based on expected load, concurrency 1-10
