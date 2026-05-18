"""VideoAssembler node — concatenates shots and applies brand overlay via ffmpeg.

Triggered by scheduler once all VideoJob rows for a script are done.
Output spec (all platforms): H.264 (libx264), AAC, 1080x1920, 30fps, CRF 23.
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime

import aiofiles
import ffmpeg
import httpx
import structlog

from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import ContentScript, VideoAsset, VideoJob
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

ASSEMBLER_VERSION = "1.0"


async def video_assembler_node(state: SignalAgentState) -> SignalAgentState:
    script_id = state.get("script_id")
    if not script_id:
        return {**state, "error": "video_assembler: missing script_id"}

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            __import__("sqlmodel", fromlist=["select"]).select(VideoJob)
            .where(VideoJob.script_id == script_id)
            .order_by(VideoJob.shot_index)
        )
        jobs = result.scalars().all()

    if not jobs:
        return {**state, "error": f"video_assembler: no VideoJob rows for {script_id}"}

    failed = [j for j in jobs if j.status == "failed"]
    if failed:
        return {**state, "error": f"video_assembler: {len(failed)} shots failed"}

    storage = pathlib.Path(settings().video_storage_path)
    shots_dir = storage / "scripts" / script_id / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = storage / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Download all shot files
    local_paths: list[pathlib.Path] = []
    for job in sorted(jobs, key=lambda j: j.shot_index):
        if job.local_path and pathlib.Path(job.local_path).exists():
            local_paths.append(pathlib.Path(job.local_path))
            continue
        if not job.video_url:
            return {**state, "error": f"video_assembler: job {job.id} has no video_url"}

        dest = shots_dir / f"shot_{job.shot_index:03d}.mp4"
        await _download(job.video_url, dest)
        local_paths.append(dest)

        async with _session_factory()() as session:
            j = await session.get(VideoJob, job.id)
            if j:
                j.local_path = str(dest)
                await session.commit()

    asset_id = str(uuid.uuid4())
    output_path = assets_dir / f"{asset_id}.mp4"

    brand_id = state.get("brand_id") or settings().default_brand_id
    _assemble(local_paths, output_path, brand_id=brand_id)

    # Calculate duration
    try:
        probe = ffmpeg.probe(str(output_path))
        duration_s = float(probe["format"].get("duration", 0.0))
    except Exception:
        duration_s = sum(j.shot_index for j in jobs) * 5.0  # rough fallback

    factory = _session_factory()
    async with factory() as session:
        asset = VideoAsset(
            id=asset_id,
            brand_id=brand_id,
            script_id=script_id,
            file_path=str(output_path),
            duration_s=duration_s,
            assembler_version=ASSEMBLER_VERSION,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(asset)

        cs = await session.get(ContentScript, script_id)
        if cs:
            cs.status = "done"
            session.add(cs)
        await session.commit()

    log.info(
        "video_assembler.done",
        asset_id=asset_id,
        duration_s=duration_s,
        output=str(output_path),
    )
    return {**state, "asset_id": asset_id, "asset_path": str(output_path)}


def _assemble(
    shot_paths: list[pathlib.Path],
    output: pathlib.Path,
    brand_id: str | None = None,
) -> None:
    """FFmpeg pipeline: concat → brand overlay → output spec."""
    bc = brand_config(brand_id).get("brand", {})
    watermark_path = bc.get("watermark_path", "assets/brand/mascot-128.png")

    # Build input streams
    inputs = [ffmpeg.input(str(p)) for p in shot_paths]

    # Concatenate video streams
    if len(inputs) == 1:
        concat = inputs[0].video
    else:
        concat = ffmpeg.concat(*[i.video for i in inputs], v=1, a=0)

    # Scale to 1080x1920 (9:16 Shorts spec), pad if needed
    scaled = concat.filter(
        "scale",
        w=1080,
        h=1920,
        force_original_aspect_ratio="decrease",
    ).filter(
        "pad",
        w=1080,
        h=1920,
        x="(ow-iw)/2",
        y="(oh-ih)/2",
        color="0x0a0a0f",
    )

    # Brand overlay: cobra watermark bottom-right, 15% width, 80% opacity
    if pathlib.Path(watermark_path).exists():
        watermark = (
            ffmpeg.input(watermark_path)
            .filter("scale", w="iw*0.15", h="-1")
            .filter("format", "rgba")
            .filter("colorchannelmixer", aa=0.8)
        )
        video = ffmpeg.overlay(
            scaled,
            watermark,
            x="main_w - overlay_w - 20",
            y="main_h - overlay_h - 20",
        )
    else:
        video = scaled

    # Color grade toward dark base + neon green
    graded = video.filter(
        "curves",
        master="0/0 0.9/1",
    ).filter(
        "colorbalance",
        rs=-0.05, gs=0.05, bs=-0.05,   # push slightly green in shadows
    )

    # Silent audio track (shots may have no audio)
    silence = ffmpeg.input("anullsrc=r=44100:cl=stereo", format="lavfi", t=999)

    ffmpeg.output(
        graded,
        silence,
        str(output),
        vcodec="libx264",
        acodec="aac",
        crf=23,
        preset="fast",
        r=30,
        pix_fmt="yuv420p",
        shortest=None,
    ).overwrite_output().run(quiet=True)


async def _download(url: str, dest: pathlib.Path) -> None:
    if url.startswith("file://"):
        src = pathlib.Path(url[7:])
        if src.exists():
            async with aiofiles.open(src, "rb") as f:
                data = await f.read()
            async with aiofiles.open(dest, "wb") as f:
                await f.write(data)
        return

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async with aiofiles.open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    await f.write(chunk)
