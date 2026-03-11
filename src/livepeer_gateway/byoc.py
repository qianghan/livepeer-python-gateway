"""
BYOC (Bring Your Own Capability) job submission for the Livepeer network.

Provides a simple synchronous API to submit inference requests (image generation,
video generation, music, etc.) to a Livepeer BYOC orchestrator.

Usage:
    from livepeer_gateway import submit_byoc_job, ByocJobRequest

    # Direct to orchestrator (offchain, no payment):
    result = submit_byoc_job(
        orch_url="https://34.134.195.88:8935",
        req=ByocJobRequest(capability="nano-banana", payload={"prompt": "a cat"}),
    )
    print(result.data)  # parsed JSON response

    # With discovery:
    result = submit_byoc_job(
        discovery_url="https://discovery.example.com",
        req=ByocJobRequest(capability="recraft-v4", payload={"prompt": "sunset"}),
    )
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .orchestrator import _http_origin, _parse_http_url, discover_orchestrators
from .errors import LivepeerGatewayError, NoOrchestratorAvailableError, OrchestratorRejection

_LOG = logging.getLogger(__name__)

# Reusable SSL context (skip verification for self-signed certs)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ByocJobRequest:
    """A BYOC job request to submit to the network."""

    capability: str
    """Capability name (e.g. 'nano-banana', 'recraft-v4', 'ltx-t2v-23')."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Request body payload (sent as JSON)."""

    timeout_seconds: int = 300
    """Maximum time the orchestrator should wait for the worker response."""

    job_id: Optional[str] = None
    """Optional job ID. Auto-generated if not provided."""

    parameters: Optional[dict[str, Any]] = None
    """Optional job parameters (orchestrator filtering, video ingress/egress)."""


@dataclass
class ByocJobResponse:
    """Response from a BYOC job submission."""

    data: Any
    """Parsed JSON response body from the orchestrator/worker."""

    status_code: int = 200
    """HTTP status code."""

    headers: dict[str, str] = field(default_factory=dict)
    """Response headers (includes Livepeer-Balance, etc.)."""

    orchestrator_url: Optional[str] = None
    """The orchestrator URL that processed this request."""

    raw_body: bytes = b""
    """Raw response body bytes."""

    @property
    def balance(self) -> Optional[str]:
        return self.headers.get("Livepeer-Balance") or self.headers.get("livepeer-balance")

    @property
    def images(self) -> list[dict]:
        """Extract images from response (convenience)."""
        if isinstance(self.data, dict):
            return self.data.get("images", [])
        return []

    @property
    def image_url(self) -> Optional[str]:
        """Extract first image URL from response."""
        for img in self.images:
            if "url" in img:
                return img["url"]
        if isinstance(self.data, dict):
            return self.data.get("image_url") or self.data.get("url")
        return None

    @property
    def video_url(self) -> Optional[str]:
        """Extract video URL from response."""
        if not isinstance(self.data, dict):
            return None
        if "video" in self.data:
            vid = self.data["video"]
            return vid.get("url") if isinstance(vid, dict) else vid
        return self.data.get("video_url") or self.data.get("url")

    @property
    def audio_url(self) -> Optional[str]:
        """Extract audio URL from response."""
        if not isinstance(self.data, dict):
            return None
        if "audio" in self.data:
            aud = self.data["audio"]
            return aud.get("url") if isinstance(aud, dict) else aud
        if "audio_file" in self.data:
            af = self.data["audio_file"]
            return af.get("url") if isinstance(af, dict) else af
        return self.data.get("url")


# ---------------------------------------------------------------------------
# Header building
# ---------------------------------------------------------------------------

def _build_livepeer_header(req: ByocJobRequest, job_id: str) -> str:
    """Build the base64-encoded Livepeer job request header."""
    job_request = {
        "id": job_id,
        "request": json.dumps(req.payload),
        "capability": req.capability,
        "timeout_seconds": req.timeout_seconds,
    }
    if req.parameters:
        job_request["parameters"] = json.dumps(req.parameters)
    return base64.b64encode(json.dumps(job_request).encode()).decode()


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def submit_byoc_job(
    req: ByocJobRequest,
    *,
    orch_url: Optional[Sequence[str] | str] = None,
    discovery_url: Optional[str] = None,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_headers: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> ByocJobResponse:
    """
    Submit a BYOC job request to the Livepeer network.

    Discovers an orchestrator (or uses explicit orch_url), builds the Livepeer
    header, and POSTs the request to /process/request/{capability}.

    Args:
        req: The job request (capability, payload, timeout).
        orch_url: Direct orchestrator URL(s). Highest priority.
        discovery_url: Discovery endpoint to find orchestrators.
        signer_url: Remote signer URL (also used for discovery fallback).
        signer_headers: Headers for signer requests.
        discovery_headers: Headers for discovery requests.
        timeout: HTTP request timeout in seconds. Defaults to req.timeout_seconds.

    Returns:
        ByocJobResponse with parsed result data.

    Raises:
        NoOrchestratorAvailableError: No orchestrator could process the request.
        LivepeerGatewayError: Network or protocol error.
    """
    job_id = req.job_id or str(uuid.uuid4())
    http_timeout = timeout or req.timeout_seconds

    # Discover orchestrators
    orch_list = _resolve_orchestrators(
        orch_url=orch_url,
        discovery_url=discovery_url,
        signer_url=signer_url,
        signer_headers=signer_headers,
        discovery_headers=discovery_headers,
    )

    _LOG.info("BYOC job %s: capability=%s, orchestrators=%s", job_id, req.capability, orch_list)

    # Build headers
    livepeer_hdr = _build_livepeer_header(req, job_id)
    body = json.dumps(req.payload).encode("utf-8")

    # Try each orchestrator
    rejections: list[OrchestratorRejection] = []

    for orch in orch_list:
        orch_origin = _http_origin(orch)
        url = f"{orch_origin}/process/request/{req.capability}"

        headers = {
            "Content-Type": "application/json",
            "Livepeer": livepeer_hdr,
            "Livepeer-Capability": req.capability,
        }

        http_req = Request(url, data=body, headers=headers, method="POST")

        _LOG.info("BYOC job %s: trying orchestrator %s", job_id, orch_origin)

        try:
            with urlopen(http_req, timeout=http_timeout, context=_ssl_ctx) as resp:
                raw_body = resp.read()
                resp_headers = {k: v for k, v in resp.headers.items()}

                try:
                    data = json.loads(raw_body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    data = raw_body

                return ByocJobResponse(
                    data=data,
                    status_code=resp.status,
                    headers=resp_headers,
                    orchestrator_url=orch_origin,
                    raw_body=raw_body,
                )

        except HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            reason = f"HTTP {e.code}: {err_body}"
            _LOG.warning("BYOC job %s: orchestrator %s rejected: %s", job_id, orch_origin, reason)

            # Non-retryable (4xx except 408/429)
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise LivepeerGatewayError(
                    f"BYOC job rejected by orchestrator {orch_origin}: {reason}"
                ) from e

            rejections.append(OrchestratorRejection(url=orch_origin, reason=reason))

        except (URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("BYOC job %s: orchestrator %s unreachable: %s", job_id, orch_origin, reason)
            rejections.append(OrchestratorRejection(url=orch_origin, reason=reason))

    raise NoOrchestratorAvailableError(rejections=rejections)


def list_capabilities(
    adapter_url: str,
    *,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """
    List capabilities registered on an adapter.

    Args:
        adapter_url: Base URL of the inference adapter (e.g. http://34.134.195.88:9090).
        timeout: HTTP timeout.

    Returns:
        List of capability dicts with 'name', 'model_id', 'capacity' keys.
    """
    url = f"{adapter_url.rstrip('/')}/capabilities"
    http_req = Request(url, headers={"Accept": "application/json"})

    try:
        with urlopen(http_req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("capabilities", [])
    except Exception as e:
        _LOG.warning("Failed to list capabilities from %s: %s", adapter_url, e)
        raise LivepeerGatewayError(f"Failed to list capabilities: {e}") from e


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_orchestrators(
    *,
    orch_url: Optional[Sequence[str] | str] = None,
    discovery_url: Optional[str] = None,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_headers: Optional[dict[str, str]] = None,
) -> list[str]:
    """Resolve orchestrator list from various sources."""
    # Direct orchestrator URL(s)
    if orch_url is not None:
        if isinstance(orch_url, str):
            urls = [u.strip() for u in orch_url.split(",") if u.strip()]
        else:
            urls = [u.strip() for u in orch_url if isinstance(u, str) and u.strip()]
        if urls:
            return urls

    # Use discovery
    if discovery_url or signer_url:
        return discover_orchestrators(
            discovery_url=discovery_url,
            signer_url=signer_url,
            signer_headers=signer_headers,
            discovery_headers=discovery_headers,
        )

    raise LivepeerGatewayError(
        "submit_byoc_job requires orch_url, discovery_url, or signer_url"
    )
