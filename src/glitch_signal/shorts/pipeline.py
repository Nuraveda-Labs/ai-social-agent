"""End-to-end YouTube Shorts pipeline: topic → mp4.

CLI today; Discord HITL preview + YouTube upload land as next layers.

Usage:
    python -m glitch_signal.shorts.pipeline \\
        --brand glitch_founder \\
        --topic "the methodology bug in my AI ads agent" \\
        --duration 45

Output:
    /var/lib/ai-social-media-agent/videos/shorts/<brand>/<uuid>.mp4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

import structlog

from glitch_signal.shorts.assembler import assemble
from glitch_signal.shorts.captions import (
    build_ass_subtitles,
    transcribe_words,
    words_json_dump,
)
from glitch_signal.shorts.motion import animate_all
from glitch_signal.shorts.script_writer import write_script
from glitch_signal.shorts.visuals import render_segments
from glitch_signal.shorts.voice import render_voiceover

log = structlog.get_logger("shorts.pipeline")


async def make_short(
    *,
    brand_id: str,
    topic: str,
    target_seconds: int = 45,
    quality: str = "high",
    captions: bool = True,
) -> pathlib.Path:
    """End-to-end: topic → script → stills → motion clips + voice (parallel)
    → captions → ffmpeg compose → mp4.

    Set captions=False if you want a clean stills + motion + voice video
    without burned word-level subtitles.
    """
    t0 = time.time()
    log.info("shorts.pipeline.start", brand_id=brand_id, topic=topic[:120])

    # 1. Script
    script = await write_script(
        brand_id=brand_id, topic=topic, target_seconds=target_seconds,
    )
    log.info(
        "shorts.pipeline.script_done",
        hook=script["hook"][:80],
        segments=len(script["segments"]),
    )

    # 2. Stills + voice in parallel — they don't depend on each other
    visuals_task = asyncio.create_task(
        render_segments(brand_id=brand_id, script=script, quality=quality)
    )
    voice_task = asyncio.create_task(
        render_voiceover(brand_id=brand_id, script=script)
    )
    frame_paths, voice_path = await asyncio.gather(visuals_task, voice_task)
    log.info(
        "shorts.pipeline.stills_voice_done",
        frames=len(frame_paths),
        voice=str(voice_path.name),
    )

    # 3. Animate stills (per-clip motion via fal.ai WAN i2v) and
    #    transcribe voiceover for word-level captions — both in parallel.
    motion_task = asyncio.create_task(
        animate_all(brand_id=brand_id, still_paths=frame_paths)
    )
    if captions:
        caption_task = asyncio.create_task(transcribe_words(voice_path))
    else:
        caption_task = None
    motion_clips = await motion_task
    log.info("shorts.pipeline.motion_done", clips=len(motion_clips))

    subtitles_path = None
    if caption_task is not None:
        words = await caption_task
        if words:
            from glitch_signal.config import settings
            subtitles_dir = (
                pathlib.Path(settings().video_storage_path)
                / "shorts" / brand_id / "captions"
            )
            subtitles_dir.mkdir(parents=True, exist_ok=True)
            stem = motion_clips[0].stem.split("_")[-1]
            words_json_dump(words, subtitles_dir / f"{stem}.words.json")
            subtitles_path = build_ass_subtitles(
                words, out_path=subtitles_dir / f"{stem}.ass",
            )

    # 4. Assemble
    mp4 = await assemble(
        brand_id=brand_id,
        motion_clip_paths=motion_clips,
        voice_path=voice_path,
        subtitles_path=subtitles_path,
    )

    elapsed = time.time() - t0
    log.info(
        "shorts.pipeline.done",
        brand_id=brand_id, mp4=str(mp4),
        elapsed_s=round(elapsed, 1),
        size_kb=mp4.stat().st_size // 1024,
    )
    return mp4


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brand", default="glitch_founder",
                   choices=["glitch_executor", "glitch_founder"])
    p.add_argument("--topic", required=True,
                   help="One-line angle for the short")
    p.add_argument("--duration", type=int, default=45,
                   help="Target duration in seconds (15-60)")
    p.add_argument("--quality", default="high",
                   choices=["low", "medium", "high"],
                   help="gpt-image-2 quality tier per still")
    p.add_argument("--script-only", action="store_true",
                   help="Just write the script JSON; skip visuals + voice + ffmpeg")
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()
    if args.script_only:
        async def _just_script() -> None:
            from glitch_signal.shorts.script_writer import write_script
            s = await write_script(
                brand_id=args.brand, topic=args.topic,
                target_seconds=args.duration,
            )
            print(json.dumps(s, indent=2))
        asyncio.run(_just_script())
        return 0

    mp4 = asyncio.run(make_short(
        brand_id=args.brand,
        topic=args.topic,
        target_seconds=args.duration,
        quality=args.quality,
    ))
    print(f"\n✓ wrote: {mp4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
