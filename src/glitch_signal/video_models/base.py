"""Abstract interface for video generation model clients."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class VideoGenerationRequest:
    prompt: str
    duration_s: int
    aspect_ratio: str = "9:16"
    style: str = "cinematic"
    seed: int | None = None
    # Optional reference image URL for image-to-video models
    reference_image_url: str | None = None


@dataclass
class VideoGenerationResult:
    api_job_id: str
    status: str                    # pending | processing | done | failed
    video_url: str | None = None
    local_path: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    raw_response: dict = field(default_factory=dict)


class VideoModel(ABC):
    """Base class for all video generation backends.

    Implementations must be safe to call concurrently — they should not
    share mutable state.
    """

    @abstractmethod
    async def generate(self, req: VideoGenerationRequest) -> VideoGenerationResult:
        """Submit a generation job. Returns immediately with a pending result.

        The caller stores `api_job_id` and polls via `poll()` separately.
        """

    @abstractmethod
    async def poll(self, api_job_id: str) -> VideoGenerationResult:
        """Check the status of a previously submitted job."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable model name used in DB rows and logs."""

    def estimate_cost(self, duration_s: int) -> float:
        """Override per model. Returns USD estimate."""
        return 0.0
