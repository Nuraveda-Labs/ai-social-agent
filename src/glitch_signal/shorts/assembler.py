"""ffmpeg compositor v2: motion clips + voiceover + burned captions → mp4.

Inputs (all per-segment, in order):
  - motion_clip_paths: 5-second mp4s from motion.animate_all (one per
    still — already has real movement, replaces the v1 Ken Burns zoom)
  - voice_path: full ElevenLabs mp3 of hook + segments + cta
  - subtitles_path: optional .ass file with word-level captions

Pipeline:
  1. Each motion clip is trimmed to its allotted duration (the audio's
     natural pacing decides per-segment time).
  2. Clips are scaled / padded to 1080x1920 if needed (most i2v models
     output 1024x576-ish, we re-scale + center-pad).
  3. Concat all clips, mux with the voiceover, burn captions.
  4. Output: libx264 yuv420p mp4 with faststart.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import shlex
import subprocess
import uuid

import structlog

from glitch_signal.config import settings

log = structlog.get_logger(__name__)

W, H = 1080, 1920


async def assemble(
    *,
    brand_id: str,
    motion_clip_paths: list[pathlib.Path],
    voice_path: pathlib.Path,
    subtitles_path: pathlib.Path | None = None,
    out_path: pathlib.Path | None = None,
) -> pathlib.Path:
    """Concat motion clips + voiceover + (optional) captions → 1080x1920 mp4."""
    s = settings()
    out_dir = pathlib.Path(s.video_storage_path) / "shorts" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_path or (out_dir / f"{uuid.uuid4().hex}.mp4")

    if not motion_clip_paths:
        raise RuntimeError("no motion clips supplied")

    audio_seconds = await _probe_duration(voice_path)
    if audio_seconds <= 0:
        raise RuntimeError(f"voice file has no duration: {voice_path}")

    # Distribute audio time across clips. Hook + CTA get a slight bonus
    # so the bookend frames breathe.
    n = len(motion_clip_paths)
    base = audio_seconds / n
    durations = [base] * n
    bonus = min(0.5, base * 0.15)
    if n >= 3:
        durations[0] += bonus
        durations[-1] += bonus
    total = sum(durations)
    durations = [d * audio_seconds / total for d in durations]

    log.info(
        "shorts.assemble.plan",
        brand_id=brand_id,
        clips=n,
        audio_seconds=round(audio_seconds, 2),
        per_clip=[round(d, 2) for d in durations],
        captions=bool(subtitles_path),
    )

    # Build ffmpeg filter graph: scale + pad each clip to 1080x1920,
    # trim to its target duration, then concat.
    inputs: list[str] = []
    filter_parts: list[str] = []
    for i, (clip, dur) in enumerate(zip(motion_clip_paths, durations, strict=True)):
        inputs += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", str(clip)]
        filter_parts.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,fps=30,format=yuv420p[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vraw]")

    if subtitles_path and subtitles_path.exists():
        # Burn the .ass file. The path needs to be escaped for ffmpeg's
        # filter syntax (colons / commas inside the value are bad).
        sub = str(subtitles_path).replace(":", r"\:").replace(",", r"\,")
        filter_parts.append(f"[vraw]subtitles='{sub}'[vout]")
    else:
        filter_parts.append("[vraw]copy[vout]")

    inputs += ["-i", str(voice_path)]
    audio_idx = n

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-map", f"{audio_idx}:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]

    log.info("shorts.assemble.ffmpeg.exec", cmd_preview=" ".join(shlex.quote(x) for x in cmd[:6]))
    await _run(cmd)
    log.info(
        "shorts.assemble.done",
        path=str(out_path),
        size_kb=out_path.stat().st_size // 1024,
        seconds=round(audio_seconds, 2),
    )
    return out_path


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

async def _probe_duration(path: pathlib.Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return 0.0
    try:
        data = json.loads(stdout.decode())
        return float(data["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr.decode(errors="replace") or "")[-1500:]
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=None, stderr=tail,
        )
