"""VideoGenerator node — dispatches VideoJob rows to video model APIs.

Does NOT block waiting for completion. Returns immediately after submitting
all shots. The scheduler/queue.py polling loop handles completion.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog

from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import settings
from glitch_signal.db.models import ContentScript, VideoJob
from glitch_signal.db.session import _session_factory
from glitch_signal.video_models.base import VideoGenerationRequest
from glitch_signal.video_models.kling import get_model

log = structlog.get_logger(__name__)


async def video_generator_node(state: SignalAgentState) -> SignalAgentState:
    script_id = state.get("script_id")
    routed_shots: list[dict] = state.get("routed_shots", [])
    brand_id = state.get("brand_id") or settings().default_brand_id

    if not script_id or not routed_shots:
        return {**state, "error": "video_generator: missing script_id or routed_shots"}

    factory = _session_factory()
    async with factory() as session:
        job_ids = await _dispatch_shots(session, script_id, routed_shots, brand_id=brand_id)

        # Mark ContentScript as generating
        cs = await session.get(ContentScript, script_id)
        if cs:
            cs.status = "generating"
            session.add(cs)
        await session.commit()

    log.info("video_generator.dispatched", script_id=script_id, n_jobs=len(job_ids))
    return {**state, "video_job_ids": job_ids, "all_shots_done": False}


async def _dispatch_shots(
    session, script_id: str, routed_shots: list[dict], brand_id: str
) -> list[str]:
    job_ids: list[str] = []

    for i, shot in enumerate(routed_shots):
        model_name = shot.get("model", "kling_2")
        duration_s = int(shot.get("duration_s", 5))
        prompt = str(shot.get("visual", ""))

        model = get_model(model_name)
        req = VideoGenerationRequest(
            prompt=prompt,
            duration_s=duration_s,
            style=shot.get("model_hint", "cinematic"),
        )

        result = await model.generate(req)

        job = VideoJob(
            id=str(uuid.uuid4()),
            brand_id=brand_id,
            script_id=script_id,
            shot_index=i,
            model=model_name,
            prompt=prompt,
            api_job_id=result.api_job_id,
            status="dispatched" if result.status in ("pending", "processing") else result.status,
            cost_usd=result.cost_usd,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            last_error=result.error,
        )
        session.add(job)
        job_ids.append(job.id)

        log.info(
            "video_generator.shot_dispatched",
            job_id=job.id,
            model=model_name,
            api_job_id=result.api_job_id,
            shot_index=i,
        )

    return job_ids
