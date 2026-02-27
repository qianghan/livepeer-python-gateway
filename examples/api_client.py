"""
Sample client for the Livepeer Gateway serverless API.

Demonstrates the full lifecycle of AI video inference via the HTTP/WebSocket API:
  1. Health check
  2. Start a job (with optional model params)
  3. Stream webcam or synthetic frames over WebSocket
  4. Receive AI-processed frames and save/display them
  5. Send control messages mid-stream
  6. Subscribe to SSE events
  7. Stop the job and clean up

Works against a local server or a remote Cloud Run deployment.

Requirements:
    pip install aiohttp pillow

Usage:
    # Minimal — stream synthetic frames through the "noop" model:
    python examples/api_client.py

    # With webcam, custom model, and API key:
    python examples/api_client.py \\
        --server https://my-gateway.run.app \\
        --api-key sk-abc123 \\
        --model my-ai-model \\
        --webcam \\
        --save-output ./output_frames

    # Send control messages mid-stream:
    python examples/api_client.py --control '{"prompt": "make it cyberpunk"}'
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import struct
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("api_client")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(api_key: Optional[str]) -> dict[str, str]:
    """Build request headers with optional API key."""
    h: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        h["X-API-Key"] = api_key
    return h


def _synthetic_jpeg(seq: int, width: int = 640, height: int = 480) -> bytes:
    """Generate a synthetic JPEG frame with a moving gradient.

    Useful when no webcam is available — the pattern changes each frame
    so you can visually confirm the pipeline is working.
    """
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    offset = (seq * 4) % width
    for y in range(height):
        for x in range(width):
            r = (x + offset) % 256
            g = (y + seq * 2) % 256
            b = (x + y + seq) % 256
            pixels[x, y] = (r, g, b)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _webcam_jpeg(cap, width: int = 640, height: int = 480) -> Optional[bytes]:
    """Capture one JPEG frame from an OpenCV VideoCapture."""
    import cv2  # imported lazily so the script works without opencv

    ret, frame = cap.read()
    if not ret:
        return None
    frame = cv2.resize(frame, (width, height))
    _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpeg.tobytes()


# ---------------------------------------------------------------------------
# API interactions
# ---------------------------------------------------------------------------


async def check_health(session: aiohttp.ClientSession, server: str) -> dict:
    """GET /health — verify the server is reachable."""
    async with session.get(f"{server}/health") as resp:
        resp.raise_for_status()
        data = await resp.json()
        _LOG.info(
            "Health OK — active_jobs=%d, version=%s",
            data["active_jobs"],
            data.get("version", "?"),
        )
        return data


async def list_jobs(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
) -> list[dict]:
    """GET /jobs — list active jobs."""
    async with session.get(f"{server}/jobs", headers=_headers(api_key)) as resp:
        resp.raise_for_status()
        jobs = await resp.json()
        _LOG.info("Active jobs: %d", len(jobs))
        return jobs


async def start_job(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
    *,
    model_id: str = "noop",
    params: Optional[dict] = None,
    orchestrator_url: Optional[str] = None,
) -> dict:
    """POST /start-job — create a new AI inference job."""
    body: dict = {"model_id": model_id}
    if params:
        body["params"] = params
    if orchestrator_url:
        body["orchestrator_url"] = orchestrator_url

    async with session.post(
        f"{server}/start-job",
        headers=_headers(api_key),
        json=body,
    ) as resp:
        if resp.status == 429:
            data = await resp.json()
            raise RuntimeError(f"Rate limited: {data['error']}")
        resp.raise_for_status()
        data = await resp.json()
        _LOG.info(
            "Job started — id=%s  model=%s",
            data["job_id"],
            data["model_id"],
        )
        _LOG.info("  publish_url:   %s", data.get("publish_url"))
        _LOG.info("  subscribe_url: %s", data.get("subscribe_url"))
        _LOG.info("  control_url:   %s", data.get("control_url"))
        _LOG.info("  events_url:    %s", data.get("events_url"))
        return data


async def get_job(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
    job_id: str,
) -> dict:
    """GET /job/{id} — get full job status."""
    async with session.get(
        f"{server}/job/{job_id}",
        headers=_headers(api_key),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def stop_job(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
    job_id: str,
) -> None:
    """DELETE /stop-job/{id} — stop and clean up a job."""
    async with session.delete(
        f"{server}/stop-job/{job_id}",
        headers=_headers(api_key),
    ) as resp:
        resp.raise_for_status()
        _LOG.info("Job stopped: %s", job_id)


async def send_control(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
    job_id: str,
    message: dict,
) -> None:
    """POST /job/{id}/control — send a control message to the job."""
    async with session.post(
        f"{server}/job/{job_id}/control",
        headers=_headers(api_key),
        json={"message": message},
    ) as resp:
        resp.raise_for_status()
        _LOG.info("Control message sent: %s", json.dumps(message))


# ---------------------------------------------------------------------------
# SSE event subscriber
# ---------------------------------------------------------------------------


async def subscribe_events(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
    job_id: str,
) -> None:
    """GET /job/{id}/events — consume Server-Sent Events until cancelled."""
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        async with session.get(
            f"{server}/job/{job_id}/events",
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            buffer = ""
            async for chunk in resp.content.iter_any():
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    for line in event_str.strip().splitlines():
                        if line.startswith("data: "):
                            payload = line[len("data: "):]
                            event = json.loads(payload)
                            _LOG.info("EVENT: %s", json.dumps(event, indent=2))
    except asyncio.CancelledError:
        return
    except Exception:
        _LOG.debug("Events stream ended", exc_info=True)


# ---------------------------------------------------------------------------
# WebSocket frame streamer
# ---------------------------------------------------------------------------


async def stream_frames(
    session: aiohttp.ClientSession,
    server: str,
    api_key: Optional[str],
    job_id: str,
    *,
    fps: float = 24.0,
    duration: float = 10.0,
    use_webcam: bool = False,
    save_dir: Optional[Path] = None,
) -> None:
    """Open the WebSocket, send input frames, and receive AI output frames."""

    ws_base = server.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/ws/stream?job_id={job_id}"
    if api_key:
        ws_url += f"&api_key={api_key}"

    cap = None
    if use_webcam:
        import cv2

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            raise RuntimeError("Cannot open webcam")
        _LOG.info("Webcam opened")

    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        _LOG.info("Saving output frames to %s", save_dir)

    frames_sent = 0
    frames_recv = 0
    t_start = time.monotonic()

    async with session.ws_connect(ws_url) as ws:
        _LOG.info("WebSocket connected")

        # --- Output reader task ---
        async def _read_output():
            nonlocal frames_recv
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frames_recv += 1
                    if save_dir:
                        out_path = save_dir / f"frame_{frames_recv:06d}.jpg"
                        out_path.write_bytes(msg.data)
                    if frames_recv % 24 == 0:
                        elapsed = time.monotonic() - t_start
                        _LOG.info(
                            "Recv %d frames  (%.1f recv-fps)  elapsed=%.1fs",
                            frames_recv,
                            frames_recv / max(elapsed, 0.001),
                            elapsed,
                        )
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        output_task = asyncio.create_task(_read_output())

        # --- Send input frames ---
        interval = 1.0 / fps
        total_frames = int(duration * fps)

        try:
            for seq in range(total_frames):
                t0 = time.monotonic()

                if use_webcam and cap is not None:
                    jpeg = _webcam_jpeg(cap)
                    if jpeg is None:
                        _LOG.warning("Webcam returned no frame, stopping")
                        break
                else:
                    jpeg = await asyncio.to_thread(_synthetic_jpeg, seq)

                await ws.send_bytes(jpeg)
                frames_sent += 1

                if frames_sent % 24 == 0:
                    elapsed = time.monotonic() - t_start
                    _LOG.info(
                        "Sent %d frames  (%.1f send-fps)",
                        frames_sent,
                        frames_sent / max(elapsed, 0.001),
                    )

                # Pace to target FPS.
                dt = time.monotonic() - t0
                if dt < interval:
                    await asyncio.sleep(interval - dt)

        except asyncio.CancelledError:
            pass
        finally:
            _LOG.info(
                "Done sending — %d frames in %.1fs",
                frames_sent,
                time.monotonic() - t_start,
            )

        # Give the server a moment to flush remaining output frames.
        await asyncio.sleep(1.0)

        output_task.cancel()
        with suppress(asyncio.CancelledError):
            await output_task

    if cap is not None:
        cap.release()

    _LOG.info(
        "Session complete — sent=%d  recv=%d  duration=%.1fs",
        frames_sent,
        frames_recv,
        time.monotonic() - t_start,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sample client for the Livepeer Gateway serverless API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--server",
        default="http://localhost:8000",
        help="Gateway server URL (default: http://localhost:8000)",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="API key for authentication (X-API-Key header).",
    )
    p.add_argument(
        "--model",
        default="noop",
        help="Model ID for the AI pipeline (default: noop).",
    )
    p.add_argument(
        "--params",
        default=None,
        help='Model params as JSON string (e.g. \'{"prompt": "oil painting"}\').',
    )
    p.add_argument(
        "--orchestrator",
        default=None,
        help="Orchestrator URL override (host:port).",
    )
    p.add_argument(
        "--fps",
        type=float,
        default=24.0,
        help="Frames per second to send (default: 24).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Seconds to stream (default: 10).",
    )
    p.add_argument(
        "--webcam",
        action="store_true",
        help="Use webcam instead of synthetic frames (requires opencv-python).",
    )
    p.add_argument(
        "--save-output",
        default=None,
        help="Directory to save received JPEG frames.",
    )
    p.add_argument(
        "--control",
        default=None,
        help='JSON control message to send mid-stream (e.g. \'{"prompt": "neon"}\').',
    )
    p.add_argument(
        "--events",
        action="store_true",
        help="Subscribe to SSE events in the background.",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    server = args.server.rstrip("/")
    params = json.loads(args.params) if args.params else None
    control_msg = json.loads(args.control) if args.control else None
    save_dir = Path(args.save_output) if args.save_output else None

    async with aiohttp.ClientSession() as session:
        # 1. Health check
        await check_health(session, server)

        # 2. List existing jobs
        await list_jobs(session, server, args.api_key)

        # 3. Start a new job
        job = await start_job(
            session,
            server,
            args.api_key,
            model_id=args.model,
            params=params,
            orchestrator_url=args.orchestrator,
        )
        job_id = job["job_id"]

        # 4. Check job status
        status = await get_job(session, server, args.api_key, job_id)
        _LOG.info(
            "Job status — media_started=%s  has_payment=%s",
            status.get("media_started"),
            status.get("has_payment_session"),
        )

        events_task: Optional[asyncio.Task] = None
        try:
            # 5. (Optional) Subscribe to SSE events
            if args.events:
                events_task = asyncio.create_task(
                    subscribe_events(session, server, args.api_key, job_id)
                )

            # 6. Stream frames over WebSocket
            #    Send a control message after 2 seconds if requested.
            async def _run_stream():
                await stream_frames(
                    session,
                    server,
                    args.api_key,
                    job_id,
                    fps=args.fps,
                    duration=args.duration,
                    use_webcam=args.webcam,
                    save_dir=save_dir,
                )

            async def _send_control_delayed():
                if control_msg is None:
                    return
                await asyncio.sleep(2.0)
                await send_control(
                    session, server, args.api_key, job_id, control_msg
                )

            await asyncio.gather(_run_stream(), _send_control_delayed())

        finally:
            # 7. Clean up
            if events_task is not None:
                events_task.cancel()
                with suppress(asyncio.CancelledError):
                    await events_task

            await stop_job(session, server, args.api_key, job_id)


if __name__ == "__main__":
    asyncio.run(main())
