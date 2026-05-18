"""Generate one 1080x1920 still per script segment.

Uses the same gpt-image-2 path the carousel pipeline uses, just at
9:16 aspect. Each segment gets its own prompt — `visual` from
script_writer's output — and we wrap with brand chrome rules so the
deck reads as a set, not a scrapbook.
"""
from __future__ import annotations

import asyncio
import pathlib
import uuid

import structlog

from glitch_signal.config import settings
from glitch_signal.media.image_gen import generate_designed_image

log = structlog.get_logger(__name__)


_SEGMENT_CHROME = (
    "9:16 vertical (1080x1920). Dark editorial tech aesthetic — true black "
    "background with very faint neon-green circuit-line texture at 4% "
    "opacity, concentrated in the corners. Top-left, 60px from each edge: "
    "a 4px-wide x 36px-tall vertical bar in bright neon green #00ff88, "
    "immediately followed by uppercase monospace 18pt white text "
    "'GLITCH · EXECUTOR'. Bottom of the frame, 50px from the bottom edge "
    "with 80px side margins: a thin 3px horizontal track in dim gray "
    "#2a2a2a. NO humans, faces, hands, or photographs. NO clipart or "
    "stock vectors. Clean geometric sans-serif typography. Perfect "
    "spelling on every word."
)


async def render_segments(
    *,
    brand_id: str,
    script: dict,
    out_dir: pathlib.Path | None = None,
    quality: str = "high",
) -> list[pathlib.Path]:
    """Render one PNG per segment, return ordered list of paths.

    Hook + CTA also get rendered as their own segments — bookend stills.
    Final order: [hook, *segments, cta]
    """
    s = settings()
    out_dir = out_dir or (
        pathlib.Path(s.video_storage_path) / "shorts" / brand_id / "frames"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build the ordered list of (label, prompt) tuples
    plan: list[tuple[str, str]] = [
        ("hook", _wrap_prompt(_hook_visual(script), is_hero=True)),
    ]
    for i, seg in enumerate(script.get("segments", []), start=1):
        plan.append((f"seg_{i:02d}", _wrap_prompt(seg["visual"])))
    plan.append(
        ("cta", _wrap_prompt(_cta_visual(script.get("cta", "")), is_hero=True))
    )

    async def _one(label: str, prompt: str) -> pathlib.Path:
        path = await generate_designed_image(
            prompt=prompt,
            brand_id=brand_id,
            aspect="4:5",  # closest tall preset; assembler will pad to 9:16
            quality=quality,
        )
        # Move into our shorts/frames dir so we don't pollute the carousel cache
        target = out_dir / f"{label}_{uuid.uuid4().hex[:8]}.png"
        path.rename(target)
        log.info("shorts.visuals.rendered", label=label, path=str(target))
        return target

    paths = await asyncio.gather(*[_one(label, prompt) for label, prompt in plan])
    return list(paths)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _wrap_prompt(visual: str, *, is_hero: bool = False) -> str:
    """Add brand chrome + 9:16 aspect rules around the per-segment prompt."""
    extra = (
        "Hero composition: stronger focal element, slightly higher visual "
        "weight than body slides. " if is_hero else ""
    )
    return f"{extra}{visual}\n\n{_SEGMENT_CHROME}"


def _hook_visual(script: dict) -> str:
    """Synthesize a visual for the hook beat from the hook line itself."""
    hook = (script.get("hook") or "").strip()
    return (
        f"A hero opener still for a short-form video. Center the frame on "
        f"oversized 96pt bold sans-serif white headline text rendered as: "
        f"\"{hook}\". A short bright neon green #00ff88 underline bar "
        f"(220px x 6px) directly beneath the headline."
    )


def _cta_visual(cta: str) -> str:
    cta = (cta or "github.com/glitch-exec-labs").strip()
    return (
        f"A closing CTA still. Center the frame on 56pt bold white text "
        f"saying \"{cta}\" rendered cleanly in monospace neon-green for "
        f"the URL portion. A short bright #00ff88 underline accent above "
        f"the URL. Quiet sign-off feel — slight upward energy."
    )
