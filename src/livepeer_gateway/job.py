"""
Unified job submission interface for the Livepeer network.

Auto-detects whether to use BYOC (batch inference) or LV2V (live video streaming)
based on the capability requested, and returns a common LivepeerJob result.

Usage:
    from livepeer_gateway import submit_job

    # BYOC (batch) -- auto-detected from capability name:
    job = submit_job("nano-banana", {"prompt": "a cat"}, orch_url="https://orch:8935")
    print(job.image_url)

    # LV2V (streaming) -- auto-detected from capability name:
    job = submit_job("live-video-to-video", {"model_id": "sdxl-v2v"},
                     orch_url="https://orch:8935", signer_url="https://signer:8935")
    print(job.publish_url)  # send frames here
    async for frame in job.media_output():
        process(frame)

    # Force a specific mode:
    job = submit_job("my-custom-cap", payload, orch_url=url, mode="byoc")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .byoc import ByocJobRequest, ByocJobResponse, submit_byoc_job
from .errors import LivepeerGatewayError
from .lv2v import LiveVideoToVideo, StartJobRequest, start_lv2v

_LOG = logging.getLogger(__name__)

# Capabilities that route to LV2V (streaming) rather than BYOC (batch).
# Anything not in this set defaults to BYOC.
LV2V_CAPABILITIES = frozenset({
    "live-video-to-video",
    "lv2v",
    "live_video_to_video",
})


def _is_lv2v(capability: str) -> bool:
    """Check if a capability should use the LV2V streaming path."""
    return capability.lower().replace("_", "-") in LV2V_CAPABILITIES


@dataclass
class LivepeerJob:
    """
    Unified job result for both BYOC and LV2V.

    For BYOC jobs: result data is immediately available via .data, .image_url, etc.
    For LV2V jobs: stream handles are available via .stream, .publish_url, etc.

    Check .job_type ("byoc" or "lv2v") or .is_streaming to determine which fields
    are populated.
    """

    job_type: str
    """'byoc' for batch inference, 'lv2v' for live video streaming."""

    capability: str
    """The capability that was requested."""

    orchestrator_url: Optional[str] = None
    """The orchestrator URL that processed (or is processing) this job."""

    # -- BYOC fields (populated when job_type == "byoc") --

    _byoc_response: Optional[ByocJobResponse] = field(default=None, repr=False)

    # -- LV2V fields (populated when job_type == "lv2v") --

    _lv2v_job: Optional[LiveVideoToVideo] = field(default=None, repr=False)

    # ---- Common properties ----

    @property
    def is_streaming(self) -> bool:
        """True if this is a streaming (LV2V) job."""
        return self.job_type == "lv2v"

    @property
    def data(self) -> Any:
        """Parsed response data (BYOC only). Returns None for LV2V."""
        if self._byoc_response:
            return self._byoc_response.data
        if self._lv2v_job:
            return self._lv2v_job.raw
        return None

    @property
    def balance(self) -> Optional[str]:
        """Livepeer balance header (BYOC only)."""
        return self._byoc_response.balance if self._byoc_response else None

    # ---- BYOC convenience properties ----

    @property
    def image_url(self) -> Optional[str]:
        """First image URL from a BYOC response."""
        return self._byoc_response.image_url if self._byoc_response else None

    @property
    def video_url(self) -> Optional[str]:
        """Video URL from a BYOC response."""
        return self._byoc_response.video_url if self._byoc_response else None

    @property
    def audio_url(self) -> Optional[str]:
        """Audio URL from a BYOC response."""
        return self._byoc_response.audio_url if self._byoc_response else None

    @property
    def images(self) -> list[dict]:
        """Image list from a BYOC response."""
        return self._byoc_response.images if self._byoc_response else []

    @property
    def status_code(self) -> Optional[int]:
        """HTTP status code (BYOC only)."""
        return self._byoc_response.status_code if self._byoc_response else None

    @property
    def headers(self) -> dict[str, str]:
        """Response headers (BYOC only)."""
        return self._byoc_response.headers if self._byoc_response else {}

    # ---- LV2V convenience properties ----

    @property
    def stream(self) -> Optional[LiveVideoToVideo]:
        """The LV2V stream object (LV2V only). Use for media I/O."""
        return self._lv2v_job

    @property
    def publish_url(self) -> Optional[str]:
        """URL to publish input video frames (LV2V only)."""
        return self._lv2v_job.publish_url if self._lv2v_job else None

    @property
    def subscribe_url(self) -> Optional[str]:
        """URL to subscribe to output video frames (LV2V only)."""
        return self._lv2v_job.subscribe_url if self._lv2v_job else None

    @property
    def control_url(self) -> Optional[str]:
        """URL for control messages (LV2V only)."""
        return self._lv2v_job.control_url if self._lv2v_job else None

    @property
    def events_url(self) -> Optional[str]:
        """URL for event subscription (LV2V only)."""
        return self._lv2v_job.events_url if self._lv2v_job else None

    @property
    def control(self):
        """Control message interface (LV2V only)."""
        return self._lv2v_job.control if self._lv2v_job else None

    @property
    def events(self):
        """Events subscription interface (LV2V only)."""
        return self._lv2v_job.events if self._lv2v_job else None

    @property
    def manifest_id(self) -> Optional[str]:
        """Manifest ID (LV2V only)."""
        return self._lv2v_job.manifest_id if self._lv2v_job else None

    def start_media(self, config):
        """Start media publishing (LV2V only). See LiveVideoToVideo.start_media()."""
        if not self._lv2v_job:
            raise LivepeerGatewayError("start_media() is only available for LV2V jobs")
        return self._lv2v_job.start_media(config)

    def media_output(self, **kwargs):
        """Create media output subscription (LV2V only). See LiveVideoToVideo.media_output()."""
        if not self._lv2v_job:
            raise LivepeerGatewayError("media_output() is only available for LV2V jobs")
        return self._lv2v_job.media_output(**kwargs)

    async def close(self) -> None:
        """Close the job and release resources (LV2V only)."""
        if self._lv2v_job:
            await self._lv2v_job.close()


def submit_job(
    capability: str,
    payload: dict[str, Any],
    *,
    orch_url: Optional[Sequence[str] | str] = None,
    mode: Optional[str] = None,
    # Common
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_url: Optional[str] = None,
    discovery_headers: Optional[dict[str, str]] = None,
    # BYOC-specific
    timeout_seconds: int = 300,
    job_id: Optional[str] = None,
    parameters: Optional[dict[str, Any]] = None,
    # LV2V-specific
    token: Optional[str] = None,
    model_id: Optional[str] = None,
    stream_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> LivepeerJob:
    """
    Submit a job to the Livepeer network, auto-detecting BYOC vs LV2V.

    For BYOC (batch inference): submits the payload and waits for the result.
    For LV2V (live streaming): starts a streaming job and returns stream handles.

    Detection logic:
    - If mode="byoc" or mode="lv2v": use that mode explicitly.
    - If capability is "live-video-to-video" (or aliases): use LV2V.
    - Otherwise: use BYOC.

    Args:
        capability: Capability name (e.g. "nano-banana", "live-video-to-video").
        payload: Request payload dict.
        orch_url: Direct orchestrator URL(s).
        mode: Force "byoc" or "lv2v". Auto-detected if None.
        signer_url: Remote signer URL (for payment and discovery).
        signer_headers: Headers for signer requests.
        discovery_url: Discovery endpoint URL.
        discovery_headers: Headers for discovery requests.
        timeout_seconds: BYOC request timeout (default 300).
        job_id: BYOC job ID (auto-generated if None).
        parameters: BYOC job parameters.
        token: LV2V base64-encoded token (contains signer/discovery URLs).
        model_id: LV2V model ID (overrides payload["model_id"] if set).
        stream_id: LV2V stream ID.
        request_id: LV2V request ID.

    Returns:
        LivepeerJob with type-appropriate fields populated.
    """
    use_lv2v = False
    if mode is not None:
        use_lv2v = mode.lower() in ("lv2v", "live-video-to-video", "streaming")
    else:
        use_lv2v = _is_lv2v(capability)

    if use_lv2v:
        return _submit_lv2v(
            capability=capability,
            payload=payload,
            orch_url=orch_url,
            signer_url=signer_url,
            signer_headers=signer_headers,
            discovery_url=discovery_url,
            discovery_headers=discovery_headers,
            token=token,
            model_id=model_id,
            stream_id=stream_id,
            request_id=request_id,
        )
    else:
        return _submit_byoc(
            capability=capability,
            payload=payload,
            orch_url=orch_url,
            signer_url=signer_url,
            signer_headers=signer_headers,
            discovery_url=discovery_url,
            discovery_headers=discovery_headers,
            timeout_seconds=timeout_seconds,
            job_id=job_id,
            parameters=parameters,
        )


def _submit_byoc(
    capability: str,
    payload: dict[str, Any],
    *,
    orch_url: Optional[Sequence[str] | str] = None,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_url: Optional[str] = None,
    discovery_headers: Optional[dict[str, str]] = None,
    timeout_seconds: int = 300,
    job_id: Optional[str] = None,
    parameters: Optional[dict[str, Any]] = None,
) -> LivepeerJob:
    """Submit a BYOC batch job and return a LivepeerJob."""
    _LOG.info("submit_job: using BYOC path for capability=%s", capability)

    resp = submit_byoc_job(
        req=ByocJobRequest(
            capability=capability,
            payload=payload,
            timeout_seconds=timeout_seconds,
            job_id=job_id,
            parameters=parameters,
        ),
        orch_url=orch_url,
        signer_url=signer_url,
        signer_headers=signer_headers,
        discovery_url=discovery_url,
        discovery_headers=discovery_headers,
    )

    return LivepeerJob(
        job_type="byoc",
        capability=capability,
        orchestrator_url=resp.orchestrator_url,
        _byoc_response=resp,
    )


def _submit_lv2v(
    capability: str,
    payload: dict[str, Any],
    *,
    orch_url: Optional[Sequence[str] | str] = None,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_url: Optional[str] = None,
    discovery_headers: Optional[dict[str, str]] = None,
    token: Optional[str] = None,
    model_id: Optional[str] = None,
    stream_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> LivepeerJob:
    """Submit an LV2V streaming job and return a LivepeerJob."""
    _LOG.info("submit_job: using LV2V path for capability=%s", capability)

    resolved_model = model_id or payload.get("model_id")
    resolved_params = payload.get("params", payload)
    # If payload has keys beyond model_id/params, treat the whole thing as params
    if "model_id" in payload:
        resolved_params = {k: v for k, v in payload.items() if k != "model_id"}
        if not resolved_params:
            resolved_params = None

    req = StartJobRequest(
        model_id=resolved_model,
        params=resolved_params if resolved_params else None,
        stream_id=stream_id or payload.get("stream_id"),
        request_id=request_id or payload.get("request_id"),
    )

    lv2v_job = start_lv2v(
        orch_url,
        req,
        token=token,
        signer_url=signer_url,
        signer_headers=signer_headers,
        discovery_url=discovery_url,
        discovery_headers=discovery_headers,
    )

    orch = None
    if lv2v_job.orchestrator_info and lv2v_job.orchestrator_info.transcoder:
        orch = lv2v_job.orchestrator_info.transcoder

    return LivepeerJob(
        job_type="lv2v",
        capability=capability,
        orchestrator_url=orch,
        _lv2v_job=lv2v_job,
    )
