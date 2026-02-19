from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Config:
    """Environment-based configuration for the web wrapper."""

    # Orchestrator URL(s), comma-separated. If empty, discovery is used.
    orchestrator_url: Optional[str] = field(
        default_factory=lambda: os.environ.get("ORCHESTRATOR_URL") or None
    )

    # Remote signer URL. If empty, runs in offchain mode.
    signer_url: Optional[str] = field(
        default_factory=lambda: os.environ.get("SIGNER_URL") or None
    )

    # Base64-encoded JSON token for authentication.
    livepeer_token: Optional[str] = field(
        default_factory=lambda: os.environ.get("LIVEPEER_TOKEN") or None
    )

    # Default model ID for jobs.
    default_model_id: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_MODEL_ID", "noop")
    )

    # FPS for media publishing.
    fps: float = field(
        default_factory=lambda: float(os.environ.get("FPS", "24"))
    )

    # JPEG quality for output frames sent to browser (0-100).
    jpeg_quality: int = field(
        default_factory=lambda: int(os.environ.get("JPEG_QUALITY", "80"))
    )

    # Host and port for uvicorn.
    host: str = field(
        default_factory=lambda: os.environ.get("HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: int(os.environ.get("PORT", "8000"))
    )
