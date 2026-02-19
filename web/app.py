"""
FastAPI web wrapper for livepeer-gateway SDK.

Bridges browser JPEG frames to the SDK's av.VideoFrame-based media pipeline
and streams AI-processed frames back over WebSocket.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

import av
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from livepeer_gateway.lv2v import LiveVideoToVideo, StartJobRequest, start_lv2v
from livepeer_gateway.media_publish import MediaPublishConfig

from .config import Config

_LOG = logging.getLogger(__name__)
_TIME_BASE = 90_000

config = Config()

# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

@dataclass
class JobState:
    job_id: str
    model_id: str
    job: LiveVideoToVideo
    created_at: float = field(default_factory=time.time)
    orchestrator_url: Optional[str] = None
    _media_started: bool = False


_jobs: dict[str, JobState] = {}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Livepeer Gateway Web Wrapper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_BROWSER_DIR = Path(__file__).resolve().parent.parent / "browser"

if _BROWSER_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=str(_BROWSER_DIR)), name="browser-static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    index_file = _BROWSER_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(str(index_file), media_type="text/html")
    return JSONResponse({"error": "Browser app not found"}, status_code=404)


@app.get("/health")
async def health():
    return {"status": "ok", "active_jobs": len(_jobs)}


@app.post("/start-job")
async def start_job(body: dict = {}):
    model_id = body.get("model_id") or config.default_model_id
    orch_url = body.get("orchestrator_url") or config.orchestrator_url

    req = StartJobRequest(model_id=model_id)

    try:
        job = await asyncio.to_thread(
            start_lv2v,
            orch_url,
            req,
            token=config.livepeer_token,
            signer_url=config.signer_url,
        )
    except Exception as e:
        _LOG.exception("Failed to start job")
        return JSONResponse({"error": str(e)}, status_code=500)

    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobState(
        job_id=job_id,
        model_id=model_id,
        job=job,
        orchestrator_url=orch_url,
    )

    _LOG.info("Started job %s (model=%s)", job_id, model_id)
    return {
        "job_id": job_id,
        "model_id": model_id,
        "publish_url": job.publish_url,
        "subscribe_url": job.subscribe_url,
    }


@app.get("/job/{job_id}")
async def get_job(job_id: str):
    state = _jobs.get(job_id)
    if not state:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return {
        "job_id": state.job_id,
        "model_id": state.model_id,
        "created_at": state.created_at,
        "orchestrator_url": state.orchestrator_url,
    }


@app.delete("/stop-job/{job_id}")
async def stop_job(job_id: str):
    state = _jobs.pop(job_id, None)
    if not state:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    try:
        await state.job.close()
    except Exception:
        _LOG.exception("Error closing job %s", job_id)

    _LOG.info("Stopped job %s", job_id)
    return {"status": "stopped", "job_id": job_id}


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket, job_id: str):
    state = _jobs.get(job_id)
    if not state:
        await ws.close(code=4004, reason="Job not found")
        return

    await ws.accept()
    _LOG.info("WebSocket connected for job %s", job_id)

    job = state.job

    # Start media publisher if not yet started.
    if not state._media_started:
        job.start_media(MediaPublishConfig(fps=config.fps))
        state._media_started = True

    media = job._media
    output = job.media_output()

    # PTS tracking for input frames.
    last_pts = 0
    last_time: Optional[float] = None
    time_base = Fraction(1, _TIME_BASE)

    async def _send_output():
        """Read AI-processed frames from SDK and send as JPEG to browser."""
        try:
            async for decoded in output.frames():
                if decoded.kind != "video":
                    continue
                try:
                    pil_img = decoded.frame.to_image()
                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=config.jpeg_quality)
                    await ws.send_bytes(buf.getvalue())
                except WebSocketDisconnect:
                    return
                except Exception:
                    _LOG.debug("Failed to send output frame", exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception:
            _LOG.debug("Output stream ended", exc_info=True)

    output_task = asyncio.create_task(_send_output())

    try:
        while True:
            data = await ws.receive_bytes()

            # Decode JPEG from browser into av.VideoFrame.
            try:
                pil_img = Image.open(io.BytesIO(data))
                frame = av.VideoFrame.from_image(pil_img)
            except Exception:
                _LOG.debug("Failed to decode input JPEG frame", exc_info=True)
                continue

            # Compute PTS.
            now = time.time()
            if last_time is not None:
                last_pts += int((now - last_time) * _TIME_BASE)
            else:
                last_pts = 0
            last_time = now

            frame.pts = last_pts
            frame.time_base = time_base

            await media.write_frame(frame)

    except WebSocketDisconnect:
        _LOG.info("WebSocket disconnected for job %s", job_id)
    except Exception:
        _LOG.exception("WebSocket error for job %s", job_id)
    finally:
        output_task.cancel()
        with suppress(asyncio.CancelledError):
            await output_task
        await output.close()
