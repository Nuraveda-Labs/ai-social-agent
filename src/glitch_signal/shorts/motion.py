"""Animate gpt-image-2 stills into short video clips via fal.ai WAN.

The v1 pipeline used ffmpeg Ken Burns zoom on stills — looked like one
image animating for the whole video. This module replaces that with
real per-segment motion: each still gets uploaded to fal.ai, animated
via WAN 2.2-5B image-to-video into a 5-second clip with subtle camera
movement / particle drift / element drift, downloaded as mp4.

WAN 2.2-5B is the right tradeoff today (2026-05):
  - $0.05-0.10 per 5-second clip (much cheaper than Kling Pro / Veo)
  - ~20s wall-time per clip, parallel-friendly
  - Open-source-trained, decent prompt adherence on subtle motion
  - Doesn't try to add fictional content — preserves the still's brand
    chrome and typography

For per-clip motion prompts: keep them SUBTLE. The still is already
brand-coded; we just want it alive, not "transformed."
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import uuid

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.config import settings

log = structlog.get_logger(__name__)


_MODEL = "fal-ai/wan/v2.2-5b/image-to-video"

# Per-segment motion prompts. Cycled through across clips so consecutive
# segments don't all do the same thing. Subtle — we want the still alive,
# not "transformed."
_MOTION_LIBRARY: list[str] = [
    "subtle slow camera push-in, soft light bloom, gentle particle drift in background",
    "very slow camera pan from left to right across the frame, no element changes",
    "extremely slow zoom-out from a tight crop, revealing more of the composition",
    "soft pulse on the bright accent elements, subtle glow breathing",
    "vertical slow tilt-down across the frame, smooth and cinematic",
    "very gentle parallax — foreground text steady, background subtly drifting",
    "slow zoom-in toward the central element, slight chromatic shimmer",
]


class MotionError(RuntimeError):
    pass


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=3, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, MotionError)),
)
async def animate_still(
    *,
    still_path: pathlib.Path,
    motion_prompt: str,
    out_path: pathlib.Path,
) -> pathlib.Path:
    """Generate one 5-second mp4 from a still + motion prompt."""
    s = settings()
    if not s.fal_api_key:
        raise MotionError("FAL_API_KEY not set")

    # fal_client is sync; run it on a thread.
    def _gen() -> str:
        os.environ.setdefault("FAL_KEY", s.fal_api_key)
        import fal_client
        url = fal_client.upload_file(str(still_path))
        result = fal_client.run(
            _MODEL,
            arguments={
                "image_url": url,
                "prompt": motion_prompt,
                # WAN 2.2-5B defaults to 81 frames @ 16fps = ~5s, leave defaults.
            },
        )
        video = (result or {}).get("video") or {}
        v_url = video.get("url")
        if not v_url:
            raise MotionError(f"WAN returned no video url: {result!r}")
        return v_url

    video_url = await asyncio.to_thread(_gen)

    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.get(video_url)
        if r.status_code >= 400:
            raise MotionError(f"download failed {r.status_code}: {video_url}")
        out_path.write_bytes(r.content)

    log.info(
        "shorts.motion.clip_done",
        still=still_path.name,
        out=out_path.name,
        size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


async def animate_all(
    *,
    brand_id: str,
    still_paths: list[pathlib.Path],
    out_dir: pathlib.Path | None = None,
) -> list[pathlib.Path]:
    """Animate every still in parallel, return mp4 clip paths in order."""
    s = settings()
    out_dir = out_dir or (
        pathlib.Path(s.video_storage_path) / "shorts" / brand_id / "clips"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _one(idx: int, still: pathlib.Path) -> pathlib.Path:
        prompt = _MOTION_LIBRARY[idx % len(_MOTION_LIBRARY)]
        out = out_dir / f"clip_{idx:02d}_{uuid.uuid4().hex[:8]}.mp4"
        return await animate_still(
            still_path=still, motion_prompt=prompt, out_path=out,
        )

    clips = await asyncio.gather(
        *[_one(i, p) for i, p in enumerate(still_paths)]
    )
    return list(clips)
