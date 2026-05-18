"""Kling 2.0 video generation client (Phase 1 primary model).

API docs: https://klingai.com/api/docs
Auth: Bearer token via KLING_API_KEY

Dry-run mode (DISPATCH_MODE=dry_run): returns a mock result immediately
without calling the API.
"""
from __future__ import annotations

import uuid

import httpx
import structlog

from glitch_signal.config import settings
from glitch_signal.video_models.base import (
    VideoGenerationRequest,
    VideoGenerationResult,
    VideoModel,
)

log = structlog.get_logger(__name__)

# Cost per second of generated video (approximate)
_COST_PER_SECOND_USD = 0.028


class KlingModel(VideoModel):
    """Kling 2.0 text-to-video client."""

    @property
    def name(self) -> str:
        return "kling_2"

    def estimate_cost(self, duration_s: int) -> float:
        return round(duration_s * _COST_PER_SECOND_USD, 4)

    async def generate(self, req: VideoGenerationRequest) -> VideoGenerationResult:
        if settings().is_dry_run:
            return self._mock_result(req.duration_s)

        s = settings()
        payload = {
            "model": "kling-v2",
            "prompt": req.prompt,
            "duration": req.duration_s,
            "aspect_ratio": req.aspect_ratio,
            "cfg_scale": 0.5,
        }
        if req.seed is not None:
            payload["seed"] = req.seed

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{s.kling_api_url}/v1/videos/text2video",
                headers={"Authorization": f"Bearer {s.kling_api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        task_id = data.get("data", {}).get("task_id") or data.get("task_id")
        if not task_id:
            raise ValueError(f"Kling API returned no task_id: {data}")

        log.info("kling.generate.submitted", task_id=task_id, duration_s=req.duration_s)
        return VideoGenerationResult(
            api_job_id=task_id,
            status="pending",
            cost_usd=self.estimate_cost(req.duration_s),
            raw_response=data,
        )

    async def poll(self, api_job_id: str) -> VideoGenerationResult:
        if settings().is_dry_run:
            return VideoGenerationResult(
                api_job_id=api_job_id,
                status="done",
                video_url=f"file://tests/fixtures/mock_shot_{api_job_id}.mp4",
                cost_usd=0.0,
            )

        s = settings()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{s.kling_api_url}/v1/videos/text2video/{api_job_id}",
                headers={"Authorization": f"Bearer {s.kling_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()

        task_status = data.get("data", {}).get("task_status", "")
        video_url = None

        works = data.get("data", {}).get("task_result", {}).get("videos", [])
        if works:
            video_url = works[0].get("url")

        # Kling status values: submitted | processing | succeed | failed
        if task_status == "succeed":
            status = "done"
        elif task_status == "failed":
            status = "failed"
        else:
            status = "processing"

        return VideoGenerationResult(
            api_job_id=api_job_id,
            status=status,
            video_url=video_url,
            raw_response=data,
        )

    def _mock_result(self, duration_s: int) -> VideoGenerationResult:
        mock_id = f"mock-{uuid.uuid4().hex[:8]}"
        return VideoGenerationResult(
            api_job_id=mock_id,
            status="pending",
            cost_usd=self.estimate_cost(duration_s),
        )


# Phase 2 stubs — clients added when routing is unlocked
class RunwayModel(VideoModel):
    @property
    def name(self) -> str:
        return "runway_gen4"

    def estimate_cost(self, duration_s: int) -> float:
        return round(duration_s * 0.25, 4)

    async def generate(self, req: VideoGenerationRequest) -> VideoGenerationResult:
        raise NotImplementedError("Runway Gen-4 client lands in Phase 2")

    async def poll(self, api_job_id: str) -> VideoGenerationResult:
        raise NotImplementedError("Runway Gen-4 client lands in Phase 2")


class Veo3Model(VideoModel):
    @property
    def name(self) -> str:
        return "veo_3"

    def estimate_cost(self, duration_s: int) -> float:
        return round(duration_s * 0.35, 4)

    async def generate(self, req: VideoGenerationRequest) -> VideoGenerationResult:
        raise NotImplementedError("Veo 3 client lands in Phase 2")

    async def poll(self, api_job_id: str) -> VideoGenerationResult:
        raise NotImplementedError("Veo 3 client lands in Phase 2")


class HailuoModel(VideoModel):
    @property
    def name(self) -> str:
        return "hailuo"

    def estimate_cost(self, duration_s: int) -> float:
        return round(duration_s * 0.016, 4)

    async def generate(self, req: VideoGenerationRequest) -> VideoGenerationResult:
        raise NotImplementedError("Hailuo client lands in Phase 2")

    async def poll(self, api_job_id: str) -> VideoGenerationResult:
        raise NotImplementedError("Hailuo client lands in Phase 2")


def get_model(model_name: str) -> VideoModel:
    """Factory: return the right VideoModel instance for a model name."""
    models: dict[str, VideoModel] = {
        "kling_2": KlingModel(),
        "runway_gen4": RunwayModel(),
        "veo_3": Veo3Model(),
        "hailuo": HailuoModel(),
    }
    if model_name not in models:
        raise ValueError(f"Unknown video model: {model_name!r}")
    return models[model_name]
