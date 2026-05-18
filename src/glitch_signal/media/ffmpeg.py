"""Local ffmpeg pre-publish transforms.

Why local ffmpeg and not Upload-Post's FFmpeg Editor API:
  - Free, no per-minute quota on the Basic plan
  - Zero vendor round-trip (~seconds saved per publish)
  - No async job pattern to poll — just subprocess.run
  - We already have ffmpeg on the VM

Brand configs can declare a list of transforms per canonical platform.
Each entry is either a bare transform name (string) or an object with a
`name` key plus transform-specific options:

  {
    "media_pipeline": {
      "tiktok":    [
        {"name": "replace_audio", "audio_path": "brand/audio/nmahya_bgm.mp3"}
      ],
      "instagram": []
    }
  }

Canonical platform names match Upload-Post's enum (`tiktok`, `instagram`,
`youtube`, …). Our publisher keys (`upload_post_tiktok`, `zernio_tiktok`,
plain `tiktok`) all resolve to the same canonical name via
`canonical_platform()` so the config is written once.

Transform outputs are cached next to the input file with a deterministic
filename — rerunning the pipeline for the same asset hits the cache and
avoids re-encoding.
"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
from collections.abc import Callable

import structlog

from glitch_signal.config import brand_config

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Canonical platform resolution
# ---------------------------------------------------------------------------

def canonical_platform(platform_key: str) -> str:
    """Strip publisher prefix to get Upload-Post's canonical platform name.

    upload_post_tiktok → tiktok
    zernio_tiktok      → tiktok
    tiktok             → tiktok
    youtube_shorts     → youtube
    instagram_reels    → instagram
    """
    if platform_key.startswith("upload_post_"):
        return platform_key[len("upload_post_"):]
    if platform_key.startswith("zernio_"):
        return platform_key[len("zernio_"):]
    if platform_key == "youtube_shorts":
        return "youtube"
    if platform_key == "instagram_reels":
        return "instagram"
    return platform_key


# ---------------------------------------------------------------------------
# Transform registry — each entry maps a name to an ffmpeg arg builder.
# The builder receives (input_path, output_path, options) and returns the
# ffmpeg argv starting after the binary name. `options` is the per-call
# options dict from the brand config (empty dict when the entry was a bare
# string). Keep builders pure so we can test without invoking ffmpeg.
# ---------------------------------------------------------------------------

TransformBuilder = Callable[[pathlib.Path, pathlib.Path, dict], list[str]]


def _strip_audio(
    input_path: pathlib.Path, output_path: pathlib.Path, options: dict
) -> list[str]:
    """Remux to the same video with the audio track removed.

    Used for drive-footage brands whose source videos carry licensed
    music (Meta Ads Library exports). TikTok's web player mutes any
    video with a Content-ID match, but a silent upload is watched fine.
    The mobile app plays the original music for matched content — but
    most viewers are on mobile, not worth splitting publishes.

    `-c:v copy` does zero re-encoding — this runs in a fraction of a
    second for a 30s clip.
    """
    return [
        "-y", "-nostdin",
        "-i", str(input_path),
        "-c:v", "copy",
        "-an",
        str(output_path),
    ]


def _replace_audio(
    input_path: pathlib.Path, output_path: pathlib.Path, options: dict
) -> list[str]:
    """Swap the original audio track for a brand-approved track.

    Proper fix for the Content-ID mute problem that motivated
    `strip_audio`: licensed music in the source clip triggers a mute on
    TikTok web + sometimes iOS. `strip_audio` makes the video silent
    everywhere (blunt), while `replace_audio` substitutes a royalty-free
    or brand-licensed track so the clip plays with sound on every
    platform.

    Options:
      - audio_path (required): path to the replacement audio file.
        Relative paths resolve against the repo root (CWD of the
        service). Any format ffmpeg reads is fine — mp3, m4a, wav.
      - bitrate (optional, default "128k"): AAC target bitrate. TikTok
        re-encodes anyway, so 128k LC-AAC is plenty for mobile playback.

    Behavior:
      - Video is copied (`-c:v copy`), no re-encode, so quality is
        untouched and the remux is fast.
      - Audio is looped with `-stream_loop -1` in case the track is
        shorter than the clip, then trimmed to video length via
        `-shortest`. Longer tracks are truncated the same way.
      - `-map 0:v -map 1:a` explicitly keeps only the new audio,
        discarding any audio track already in the video.
    """
    audio_rel = options.get("audio_path")
    if not audio_rel:
        raise ValueError(
            "ffmpeg.replace_audio: options.audio_path is required — "
            "declare it in media_pipeline.<platform> as "
            '{"name": "replace_audio", "audio_path": "brand/audio/<file>"}'
        )
    audio_path = pathlib.Path(audio_rel)
    if not audio_path.is_absolute():
        audio_path = pathlib.Path.cwd() / audio_path
    if not audio_path.exists():
        raise FileNotFoundError(
            f"ffmpeg.replace_audio: audio file not found at {audio_path}"
        )

    bitrate = str(options.get("bitrate") or "128k")

    return [
        "-y", "-nostdin",
        "-i", str(input_path),
        "-stream_loop", "-1",
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", bitrate,
        "-shortest",
        str(output_path),
    ]


_TRANSFORMS: dict[str, TransformBuilder] = {
    "strip_audio": _strip_audio,
    "replace_audio": _replace_audio,
}


def registered_transforms() -> list[str]:
    """Return the list of known transform names (for validation/help text)."""
    return sorted(_TRANSFORMS.keys())


# ---------------------------------------------------------------------------
# Apply transforms
# ---------------------------------------------------------------------------

async def apply_transforms(
    file_path: str,
    brand_id: str,
    platform_key: str,
) -> str:
    """Return the path to publish, running any configured transforms first.

    Resolves the brand's `media_pipeline.<canonical_platform>` list, runs
    each transform in order, and returns the final file path. If no
    transforms are configured (or the brand_id has no config) this is a
    zero-cost passthrough that returns the input path unchanged.
    """
    if not brand_id:
        return file_path

    try:
        cfg = brand_config(brand_id)
    except Exception as exc:
        log.warning(
            "ffmpeg.apply_transforms.no_brand_config",
            brand_id=brand_id,
            error=str(exc)[:200],
        )
        return file_path

    pipeline = (cfg.get("media_pipeline") or {})
    transforms = pipeline.get(canonical_platform(platform_key)) or []
    if not transforms:
        return file_path

    input_path = pathlib.Path(file_path)
    if not input_path.exists():
        raise FileNotFoundError(f"ffmpeg.apply_transforms: input missing: {file_path}")

    current = input_path
    for entry in transforms:
        name, options = _parse_entry(entry)
        builder = _TRANSFORMS.get(name)
        if builder is None:
            raise ValueError(
                f"ffmpeg.apply_transforms: unknown transform {name!r} for brand "
                f"{brand_id!r} platform {platform_key!r}. Registered: "
                f"{registered_transforms()}"
            )
        out = _output_path(current, name)
        if not out.exists():
            argv = builder(current, out, options)
            await _run_ffmpeg(argv)
            log.info(
                "ffmpeg.transform.applied",
                brand_id=brand_id,
                platform=platform_key,
                transform=name,
                input=str(current),
                output=str(out),
                output_bytes=out.stat().st_size if out.exists() else None,
            )
        else:
            log.info(
                "ffmpeg.transform.cache_hit",
                brand_id=brand_id,
                transform=name,
                output=str(out),
            )
        current = out

    return str(current)


def _parse_entry(entry: object) -> tuple[str, dict]:
    """Normalise a pipeline entry to (name, options).

    Accepts `"strip_audio"` (bare string, no options) or
    `{"name": "replace_audio", "audio_path": "..."}` (object with options).
    The options dict handed to the builder excludes the `name` key.
    """
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"ffmpeg.apply_transforms: object entry missing string 'name': {entry!r}"
            )
        return name, {k: v for k, v in entry.items() if k != "name"}
    raise ValueError(
        f"ffmpeg.apply_transforms: transform entry must be str or dict, got {type(entry).__name__}: {entry!r}"
    )


def _output_path(input_path: pathlib.Path, transform_name: str) -> pathlib.Path:
    """Deterministic cache path: sibling file with transform tag in stem.

    `/.../clip.mp4` + `strip_audio` → `/.../clip.strip_audio.mp4`

    Chained transforms produce `/.../clip.strip_audio.other.mp4`, so each
    step's output is addressable and the cache hits independently.
    """
    stem = input_path.stem
    suffix = input_path.suffix or ".mp4"
    return input_path.with_name(f"{stem}.{transform_name}{suffix}")


async def _run_ffmpeg(argv: list[str]) -> None:
    """Invoke ffmpeg with the given argv tail. Raises on non-zero exit."""
    full = ["ffmpeg", *argv]
    log.info("ffmpeg.run", argv=full)
    # Run in a thread so we never block the scheduler event loop. ffmpeg
    # is CPU-bound but typically runs in <1s for a strip_audio remux, so
    # a plain subprocess.run inside to_thread is fine — no need for
    # asyncio.create_subprocess_exec here.
    result = await asyncio.to_thread(
        subprocess.run,
        full,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:500]}"
        )
