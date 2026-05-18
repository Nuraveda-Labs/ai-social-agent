"""AI image generation via fal.ai.

Primary use: LinkedIn image posts (and later Twitter/Instagram) for text brands
that want visual pairing. Generates a PNG from a prompt and returns the local
path; the publisher uploads via upload_post.upload_photos().

Default model is `fal-ai/flux/schnell` — fast (~1-2s) and cheap
(~$0.003/image). Swap via FAL_IMAGE_MODEL in .env. All calls go through a
tenacity retry so transient network/503s don't drop an image.

Outputs land under `{settings.video_storage_path}/images/{brand_id}/` with a
UUID filename. Re-runs never overwrite; each call produces a new file.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid
from typing import Literal

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

# fal.ai's FLUX models accept a named aspect ratio. Map our semantic names
# to what the SDK expects. These are the three we actually use for social.
_ASPECT_MAP: dict[str, str] = {
    "1:1":  "square_hd",          # 1024x1024 — LinkedIn default, safe everywhere
    "4:5":  "portrait_4_3",       # LinkedIn/IG portrait
    "16:9": "landscape_16_9",     # Twitter/YouTube thumbnail
}

AspectRatio = Literal["1:1", "4:5", "16:9"]


class ImageGenError(RuntimeError):
    """Raised when fal.ai returns no image or the download fails."""


async def generate_image(
    prompt: str,
    brand_id: str,
    aspect: AspectRatio = "1:1",
) -> pathlib.Path:
    """Generate an image via fal.ai, download it, return the local path.

    Raises ImageGenError on failure. DISPATCH_MODE=dry_run short-circuits with
    a placeholder path that doesn't exist on disk (caller should skip upload
    in dry-run mode anyway).
    """
    s = settings()
    if s.is_dry_run:
        log.info("image_gen.dry_run", brand_id=brand_id, prompt=prompt[:80])
        return pathlib.Path(f"/tmp/dry-run-image-{uuid.uuid4().hex[:8]}.png")

    if not s.fal_api_key:
        raise ImageGenError("FAL_API_KEY is not set")

    out_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex}.png"

    image_url = await _generate_via_fal(prompt, aspect, model=s.fal_image_model)
    await _download(image_url, out_path)

    log.info(
        "image_gen.done",
        brand_id=brand_id,
        path=str(out_path),
        size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError, ImageGenError)),
)
async def _generate_via_fal(prompt: str, aspect: AspectRatio, model: str) -> str:
    """Call fal.ai, return the image URL. Retries on network/ImageGenError."""
    # Run the sync fal-client call off the event loop so the graph stays
    # responsive even if fal is slow.
    # fal-client reads FAL_KEY from env; settings loader sets the env var at
    # startup via pydantic_settings, but to be safe we also set it here.
    import os

    import fal_client
    if settings().fal_api_key and not os.environ.get("FAL_KEY"):
        os.environ["FAL_KEY"] = settings().fal_api_key

    image_size = _ASPECT_MAP.get(aspect, "square_hd")

    def _run() -> dict:
        return fal_client.run(
            model,
            arguments={
                "prompt": prompt,
                "image_size": image_size,
                "num_images": 1,
            },
        )

    result = await asyncio.to_thread(_run)
    images = result.get("images") or []
    if not images:
        raise ImageGenError(f"fal.ai returned no images for model={model}")

    url = images[0].get("url") if isinstance(images[0], dict) else None
    if not url:
        raise ImageGenError(f"fal.ai image had no URL: {images[0]!r}")
    return url


async def _download(url: str, out_path: pathlib.Path) -> None:
    """Stream-download an image URL to disk."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)


# ---------------------------------------------------------------------------
# Leonardo.ai — poster/illustration backgrounds
#
# Use for slide / quote-card BACKGROUND assets only — never ask Leonardo to
# render real copy. Text is rendered by Pillow on top of the background.
# This is the "AI for visuals, code for typography" pattern: AI image models
# are not layout engines, so we only ask them for the parts they're good at.
#
# Leonardo's REST API is async: POST /generations creates a job, then we
# poll GET /generations/<id> until status="COMPLETE" and image URLs appear.
# Phoenix model is poster-grade illustration; Vision XL is photoreal.
# ---------------------------------------------------------------------------

# Pixel dimensions per aspect — Leonardo accepts 512–1536 on each axis. We
# generate at 1080-line dimensions so backgrounds match Pillow slide size
# (1080×1350) without large up/down-scaling.
_LEONARDO_PX: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "4:5":  (1080, 1344),    # Leonardo rounds to 64 — 1344 ≈ 1350
    "4:3":  (1024, 768),
    "16:9": (1280, 720),
}


async def generate_background(
    prompt: str,
    brand_id: str,
    *,
    aspect: AspectRatio = "1:1",
    negative_prompt: str | None = None,
) -> pathlib.Path:
    """Generate a background image via Leonardo.ai. Returns local PNG path.

    The prompt should describe COMPOSITION, MOOD, COLOR, TEXTURE — not text.
    We hard-append a negative prompt that forbids text/letters/words so
    Leonardo doesn't bake gibberish typography in. The caller (carousel /
    quote_card) overlays real copy with Pillow afterward.

    Falls back to FLUX-via-fal if LEONARDO_API_KEY isn't set, so existing
    deployments keep working while we cut over.
    """
    s = settings()
    if s.is_dry_run:
        log.info("image_gen.bg.dry_run", brand_id=brand_id, prompt=prompt[:80])
        return pathlib.Path(f"/tmp/dry-run-bg-{uuid.uuid4().hex[:8]}.png")

    if not s.leonardo_api_key:
        # Soft fallback to FLUX so the pipeline still works without Leonardo.
        log.warning("image_gen.bg.no_leonardo_key.falling_back_to_flux")
        return await generate_image(prompt=prompt, brand_id=brand_id, aspect=aspect)

    out_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex}.png"

    image_url = await _generate_via_leonardo(
        prompt=prompt, aspect=aspect, negative_prompt=negative_prompt,
    )
    await _download(image_url, out_path)

    log.info(
        "image_gen.bg.done",
        brand_id=brand_id, provider="leonardo",
        path=str(out_path), size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


# Default negative prompt — keeps Leonardo from rendering text/UI noise that
# would clash with the Pillow overlay. Per-call override possible.
_LEONARDO_DEFAULT_NEG = (
    "text, letters, words, captions, typography, watermark, logo, "
    "signature, ui, interface, buttons, menu, low quality, blurry, "
    "jpeg artifacts, cartoon, clipart, stock photo, people, faces, hands"
)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError, ImageGenError)),
)
async def _generate_via_leonardo(
    *,
    prompt: str,
    aspect: AspectRatio,
    negative_prompt: str | None,
) -> str:
    """POST to Leonardo, poll until complete, return the first image URL.

    Total wall time typically 6-15s on Phoenix; we cap at 90s to keep the
    carousel pipeline (parallel slide gen) bounded.
    """
    s = settings()
    width, height = _LEONARDO_PX.get(aspect, (1024, 1024))
    base = s.leonardo_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {s.leonardo_api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "modelId": s.leonardo_model_id,
        "prompt": prompt,
        "negative_prompt": negative_prompt or _LEONARDO_DEFAULT_NEG,
        "width": width,
        "height": height,
        "num_images": 1,
        # Phoenix-specific knobs — safe to send to other models too, ignored.
        "alchemy": True,
        "contrast": 3.5,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{base}/generations", headers=headers, json=payload)
        if resp.status_code >= 400:
            raise ImageGenError(
                f"Leonardo POST /generations {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        gen_id = (data.get("sdGenerationJob") or {}).get("generationId")
        if not gen_id:
            raise ImageGenError(f"Leonardo: no generationId in response: {data!r}")

        # Poll for completion. Phoenix typically 6-15s; we check every 2s up to 90s.
        deadline = asyncio.get_event_loop().time() + 90
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            poll = await client.get(f"{base}/generations/{gen_id}", headers=headers)
            if poll.status_code >= 400:
                raise ImageGenError(
                    f"Leonardo GET /generations/{gen_id} {poll.status_code}: {poll.text[:400]}"
                )
            body = poll.json().get("generations_by_pk") or {}
            status = body.get("status")
            if status == "COMPLETE":
                images = body.get("generated_images") or []
                if not images:
                    raise ImageGenError(f"Leonardo COMPLETE but no images: {body!r}")
                url = images[0].get("url")
                if not url:
                    raise ImageGenError(f"Leonardo image had no URL: {images[0]!r}")
                return url
            if status == "FAILED":
                raise ImageGenError(f"Leonardo generation FAILED: {body!r}")

    raise ImageGenError(f"Leonardo generation {gen_id} timed out after 90s")


# ---------------------------------------------------------------------------
# Designed images via OpenAI gpt-image-2 (via fal.ai)
#
# Use for posts that need TEXT RENDERED INSIDE the image — quote cards,
# stat reveals, carousel slides. gpt-image-2 nails short-to-medium text
# first-try with strong design composition; FLUX-schnell is better for
# abstract backgrounds where Pillow overlays the text.
#
# Pricing (fal.ai, Apr 2026): low ≈ $0.01, medium ≈ $0.04, high ≈ $0.17.
# ---------------------------------------------------------------------------

# fal.ai's gpt-image-2 aspect ratios use named enums, not pixel dimensions.
_GPT_IMAGE_2_ASPECT: dict[str, str] = {
    "1:1":  "square_hd",
    "4:5":  "portrait_4_3",      # closest to 4:5 offered
    "4:3":  "portrait_4_3",
    "16:9": "landscape_16_9",
}

DesignQuality = Literal["low", "medium", "high"]


async def generate_designed_image(
    prompt: str,
    brand_id: str,
    *,
    aspect: AspectRatio = "1:1",
    quality: DesignQuality = "medium",
    model: str = "openai/gpt-image-2",
) -> pathlib.Path:
    """Generate a fully designed image (text-inside-image) via gpt-image-2.

    Unlike generate_image() which produces bare backgrounds for Pillow
    overlay, this function returns an image with the text already rendered
    in the composition. Use it for quote cards, carousel slides, and any
    single-image post where the typography IS the design.

    Quality tiers roughly (fal.ai):
        low    — $0.01/image — ok for drafts
        medium — $0.04/image — default, ships fine
        high   — $0.17/image — hero posts that need to land on first scroll

    Raises ImageGenError on failure. dry_run returns a placeholder path.
    """
    s = settings()
    if s.is_dry_run:
        log.info(
            "image_gen.designed.dry_run",
            brand_id=brand_id, prompt=prompt[:80], quality=quality,
        )
        return pathlib.Path(f"/tmp/dry-run-designed-{uuid.uuid4().hex[:8]}.png")

    out_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex}.png"

    # Provider routing: prefer OpenAI direct when OPENAI_API_KEY is set.
    # Direct route is the same model OpenAI publishes (gpt-image-1 in the
    # public catalog; "gpt-image-2" if/when GA), accessed via the Images
    # API. Fal.ai is the historical fallback path; we route to it only
    # when the OpenAI key isn't configured.
    if s.openai_api_key:
        await _generate_via_openai_direct(
            prompt=prompt, aspect=aspect, quality=quality, out_path=out_path,
        )
        provider = "openai-direct"
    else:
        if not s.fal_api_key:
            raise ImageGenError("Neither OPENAI_API_KEY nor FAL_API_KEY is set")
        image_url = await _generate_via_gpt_image_2(
            prompt=prompt, aspect=aspect, quality=quality, model=model,
        )
        await _download(image_url, out_path)
        provider = f"fal:{model}"

    log.info(
        "image_gen.designed.done",
        brand_id=brand_id, provider=provider, quality=quality,
        path=str(out_path), size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


# ---------------------------------------------------------------------------
# OpenAI Images API direct (preferred when OPENAI_API_KEY is set)
# ---------------------------------------------------------------------------

# OpenAI's Images API takes a discrete `size` string. Map our aspect names
# to the closest officially-supported size on gpt-image-1.
_OPENAI_SIZE_MAP: dict[str, str] = {
    "1:1":  "1024x1024",
    "4:5":  "1024x1536",   # closest portrait — used for LI carousel slides
    "4:3":  "1024x1536",
    "16:9": "1536x1024",
}

# OpenAI's image models default to b64_json output. Some accounts/models
# also support `url`; b64 is universally supported, so use that.
# gpt-image-2 is the current public model (as of Apr 2026, replaced 1).
_OPENAI_IMAGE_MODEL = "gpt-image-2"


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (httpx.HTTPError, asyncio.TimeoutError, ImageGenError)
    ),
)
async def _generate_via_openai_direct(
    *,
    prompt: str,
    aspect: AspectRatio,
    quality: DesignQuality,
    out_path: pathlib.Path,
) -> None:
    """Call OpenAI Images API directly. Decodes the b64 response, writes
    the PNG bytes to out_path."""
    import base64
    s = settings()
    api_key = s.openai_api_key
    size = _OPENAI_SIZE_MAP.get(aspect, "1024x1024")
    payload = {
        "model": _OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        raise ImageGenError(
            f"OpenAI Images API {resp.status_code}: {resp.text[:400]}"
        )
    data = resp.json().get("data") or []
    if not data:
        raise ImageGenError(f"OpenAI Images API returned no data: {resp.text[:300]}")
    b64 = data[0].get("b64_json")
    if not b64:
        # Some models return `url` instead — handle that too.
        url = data[0].get("url")
        if url:
            await _download(url, out_path)
            return
        raise ImageGenError(
            f"OpenAI Images API: no b64_json or url in response: {data[0]!r}"
        )
    out_path.write_bytes(base64.b64decode(b64))


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError, ImageGenError)),
)
async def _generate_via_gpt_image_2(
    *, prompt: str, aspect: AspectRatio, quality: DesignQuality, model: str,
) -> str:
    """fal.ai call for gpt-image-2. gpt-image-2 takes longer than FLUX
    (thinking phase), so we use a generous timeout inside fal_client.run."""
    import os

    import fal_client

    if settings().fal_api_key and not os.environ.get("FAL_KEY"):
        os.environ["FAL_KEY"] = settings().fal_api_key

    # Prefer explicit pixel dimensions over named presets — the named
    # `portrait_4_3` preset on fal returns 768x1024, which downscales blurry
    # text on LinkedIn's 1080+ render. Asking for full-HD portrait gives the
    # model more pixels to render type into.
    px_map: dict[str, dict[str, int]] = {
        "1:1":  {"width": 1024, "height": 1024},
        "4:5":  {"width": 1080, "height": 1350},   # LinkedIn carousel native
        "4:3":  {"width": 1080, "height": 1440},
        "16:9": {"width": 1280, "height": 720},
    }
    image_size: dict | str = px_map.get(aspect) or _GPT_IMAGE_2_ASPECT.get(aspect, "square_hd")

    def _run() -> dict:
        return fal_client.run(
            model,
            arguments={
                "prompt": prompt,
                "image_size": image_size,
                "quality": quality,
                "num_images": 1,
            },
        )

    result = await asyncio.to_thread(_run)
    images = result.get("images") or []
    if not images:
        raise ImageGenError(f"gpt-image-2 returned no images for prompt={prompt[:80]!r}")
    url = images[0].get("url") if isinstance(images[0], dict) else None
    if not url:
        raise ImageGenError(f"gpt-image-2 image had no URL: {images[0]!r}")
    return url
