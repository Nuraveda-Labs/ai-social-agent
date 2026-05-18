"""LinkedIn PDF carousel generator.

LinkedIn document posts (PDF carousels) are the highest-engagement format on
the platform — 24.42% avg vs ~4% for text-only. This module produces one
ready-to-upload PDF per signal for a text brand.

Rendering pipeline (May 2026 rebuild — back to template-driven):

  1. LLM           → slide structure (hook, N body slides, CTA)
  2. Leonardo      → one abstract BACKGROUND per slide (no text in prompt)
  3. Pillow        → render real typography + chrome + per-archetype graphics
                     on top of the background — code-driven layout, not AI-baked
  4. img2pdf       → stitch slide PNGs into a single PDF

Why we reverted: gpt-image-2 produced beautiful but generic AI-poster slides
with mis-spelt headlines, drifting margins, and unreadable body type at
LinkedIn's 1080+ render. Image models are not layout engines. The right
split is "AI for visuals, code for typography" — Leonardo gives us a
poster-grade background per slide; Pillow places exact text and chrome.

Output lands at `{settings.video_storage_path}/carousels/{brand_id}/<uuid>.pdf`.
Every call produces a fresh file.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import uuid
from typing import Any

import litellm
import structlog
from PIL import Image, ImageDraw, ImageFont
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from social_signal.agent.llm import pick
from social_signal.config import brand_config, settings
from social_signal.db.models import Signal
from social_signal.media.image_gen import (
    generate_background,
)

log = structlog.get_logger(__name__)

# LinkedIn recommends 4:5 for document posts — renders full-height in feed.
SLIDE_W = 1080
SLIDE_H = 1350

# Fonts shipped with most Debian/Ubuntu systems — no extra install.
_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
_FONT_REGULAR = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
_FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# Brand defaults; overridden by brand_config.brand.{accent_color, base_color}.
_DEFAULT_ACCENT = "#00ff88"
_DEFAULT_BASE = "#0a0a0f"

_SLIDE_SYSTEM = """You are writing a LinkedIn PDF carousel for a technical founder's AI lab.

Carousels on LinkedIn get 6x the engagement of text posts when they deliver
real frameworks / lessons / concrete decisions. Empty "5 tips" filler dies.

Your job: take the supplied signal (a shipped piece of work) and break it
into a tight carousel that teaches one coherent idea end-to-end.

Rules:
- Voice matches the brand voice file verbatim — no "thrilled to announce",
  no "game-changer", no emoji walls. Technical, direct, specific.
- Hook slide: one sentence ≤ 12 words that stops the scroll. Subtitle ≤ 18 words.
- Body slides: each one self-contained. Title ≤ 8 words, body ≤ 35 words.
- Every slide carries a concrete specific — prefer a DECISION or a TRADEOFF
  to a METRIC. Metrics only if the signal contained them; don't invent.
- NO slide should just repeat the hook in different words.
- CTA slide: invite people to the repo / site. One action, no ladder.

HARD GUARD RAILS — any carousel violating these will be rejected at review:
1. Do NOT claim a measured outcome (percent / ROI / reduction / growth /
   savings / revenue) unless the signal you were given explicitly contains
   that number. Describe the BUILD, not the RESULT.
2. Do NOT use marketing verbs: "reduces", "boosts", "delivers", "improves".
   Use "targets", "aims to", "was built to", "is running", "is in production".
3. Do NOT use hype adjectives: game-changing, revolutionary, cutting-edge,
   industry-leading, robust, powerful, seamless.
4. No "excited to announce" / "thrilled to share" / "proud to introduce".
5. Never promise financial outcomes. Never say "results guaranteed".

Output valid JSON only, no markdown fences, matching this schema:
{
  "hook": {"title": "<≤12 words>", "subtitle": "<≤18 words>"},
  "body": [
    {"title": "<≤8 words>", "body": "<≤35 words>"},
    ...
  ],
  "cta": {"title": "<≤10 words>", "subtitle": "<≤18 words>", "link": "<url>"}
}
"""


class CarouselError(RuntimeError):
    pass


async def generate_carousel(
    signal: Signal | None,
    brand_id: str,
    *,
    body_slides: int = 5,
    cta_link: str = "github.com/<your-org>",
    slide_data_override: dict[str, Any] | None = None,
) -> pathlib.Path:
    """Generate a LinkedIn-ready PDF carousel. Returns the PDF path.

    Two modes:
      - Signal-driven (signal set, slide_data_override=None): LLM generates
        slide content from the Signal + voice + playbook.
      - Pre-written (slide_data_override set): caller supplies the exact
        slide content as a dict matching the schema. No LLM call. Useful
        when an operator wants pixel-perfect control over a specific post.

    Total slide count = 1 (hook) + len(body) + 1 (cta). Six is the LinkedIn
    sweet spot; more than 10 loses engagement.

    Dry-run mode returns a fake path without calling fal.ai or the LLM.
    """
    s = settings()
    signal_id_for_log = signal.id if signal else "override"
    if s.is_dry_run:
        fake = pathlib.Path(f"/tmp/dry-run-carousel-{uuid.uuid4().hex[:8]}.pdf")
        log.info("carousel.dry_run", signal_id=signal_id_for_log, path=str(fake))
        return fake

    if slide_data_override is not None:
        slide_data = slide_data_override
    else:
        if signal is None:
            raise CarouselError(
                "generate_carousel: either signal or slide_data_override required"
            )
        slide_data = await _generate_slide_content(
            signal=signal,
            brand_id=brand_id,
            body_slides=body_slides,
            cta_link=cta_link,
        )
    total_slides = 1 + len(slide_data["body"]) + 1
    accent, base, secondary = _brand_colors(brand_id)

    # Build per-slide specs: (role, archetype, title, body, optional link).
    # Each spec drives BOTH a Leonardo background prompt and a Pillow render
    # pass. Backgrounds are abstract-only — text/chrome placed by Pillow.
    specs: list[dict[str, Any]] = []
    specs.append({
        "role": "hook", "archetype": "hook", "slide_num": 1,
        "title": slide_data["hook"]["title"],
        "body": slide_data["hook"]["subtitle"],
        "link": "",
    })
    archetype_cycle = ["split", "stat", "code", "asymmetric", "halo"]
    for i, body in enumerate(slide_data["body"], start=2):
        specs.append({
            "role": "body",
            "archetype": archetype_cycle[(i - 2) % len(archetype_cycle)],
            "slide_num": i,
            "title": body["title"],
            "body": body["body"],
            "link": "",
        })
    specs.append({
        "role": "cta", "archetype": "cta", "slide_num": total_slides,
        "title": slide_data["cta"]["title"],
        "body": slide_data["cta"]["subtitle"],
        "link": slide_data["cta"].get("link", ""),
    })

    # Generate backgrounds in parallel via Leonardo. Each prompt asks ONLY
    # for atmosphere — composition, color, texture — never text. Cost ≈
    # $0.02–0.05 per Phoenix call → ~$0.15/carousel for 7 slides, vs the
    # $1.19/carousel we were paying gpt-image-2.
    async def _bg(spec: dict[str, Any]) -> pathlib.Path:
        prompt = _build_background_prompt(
            role=spec["role"], archetype=spec["archetype"],
            accent=accent, base=base, secondary=secondary,
        )
        return await generate_background(
            prompt=prompt, brand_id=brand_id, aspect="4:5",
        )

    bg_paths = await asyncio.gather(*[_bg(spec) for spec in specs])

    # Render each slide: background + Pillow text/chrome/decorations.
    slide_png_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    slide_png_dir.mkdir(parents=True, exist_ok=True)
    slide_png_paths: list[pathlib.Path] = []
    for spec, bg_path in zip(specs, bg_paths):
        img = _render_slide_v2(
            background_path=bg_path,
            archetype=spec["archetype"],
            slide_num=spec["slide_num"],
            slide_total=total_slides,
            title=spec["title"],
            body=spec["body"],
            link=spec["link"],
            accent=accent, base=base, secondary=secondary,
        )
        png_path = slide_png_dir / f"slide_{uuid.uuid4().hex}.png"
        img.save(png_path, format="PNG", optimize=False)
        slide_png_paths.append(png_path)

    out_dir = pathlib.Path(s.video_storage_path) / "carousels" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    carousel_id = uuid.uuid4().hex
    pdf_path = out_dir / f"{carousel_id}.pdf"
    _compile_pdf(list(slide_png_paths), pdf_path)

    log.info(
        "carousel.done",
        brand_id=brand_id,
        signal_id=signal_id_for_log,
        path=str(pdf_path),
        slides=total_slides,
        size_kb=pdf_path.stat().st_size // 1024,
    )
    return pdf_path


# ---------------------------------------------------------------------------
# LLM: produce structured slide content
# ---------------------------------------------------------------------------

@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _generate_slide_content(
    *,
    signal: Signal,
    brand_id: str,
    body_slides: int,
    cta_link: str,
) -> dict[str, Any]:
    cfg = brand_config(brand_id)
    voice = _load_file(cfg.get("voice_prompt_path"))
    playbook = _load_file(cfg.get("platform_playbook_path"))

    system = (
        f"{voice}\n\n"
        f"---\n"
        f"{_SLIDE_SYSTEM}\n"
        f"---\n"
        f"Platform playbook (condensed):\n{playbook[:2500]}\n"
        f"---\n"
        f"Produce exactly {body_slides} body slides. CTA link: {cta_link}"
    )
    user = (
        f"Signal:\n"
        f"Source: {signal.source} ({signal.source_ref})\n"
        f"Summary: {signal.summary}\n"
        f"Novelty: {signal.novelty_score:.2f}\n\n"
        f"Write the carousel."
    )

    s_ = settings()
    tier = "smart" if (s_.openai_api_key or s_.anthropic_api_key) else "cheap"
    mc = pick(tier)
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=4096,
        **mc.kwargs,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CarouselError(f"LLM returned invalid JSON: {exc} :: {raw[:200]!r}") from exc

    if "hook" not in data or "body" not in data or "cta" not in data:
        raise CarouselError(f"LLM output missing required keys: {list(data.keys())}")
    return data


# ---------------------------------------------------------------------------
# Body-driven carousel: take a hand-written LinkedIn post body and structure
# it into slides. Used by the sheet_posting pipeline when platform is
# upload_post_linkedin — the sheet row's body becomes the post description
# and the carousel PDF is the attached document.
# ---------------------------------------------------------------------------

_BODY_TO_SLIDES_SYSTEM = """You convert a polished LinkedIn post body into carousel slide content.

The body is already good prose written in a specific brand voice. Your job is
to RESTRUCTURE it into a tight slide deck — a hook slide, 4-5 body slides,
and a CTA slide — without rewriting the voice or inventing claims.

Rules:
- Voice stays identical to the input body. Match its tone verbatim.
- Do NOT add new claims, numbers, or ideas. Pull only what's already in the body.
- Do NOT fabricate metrics. If the body doesn't state a number, neither do you.
- Hook slide: lift the sharpest single idea from the body. Title ≤ 12 words.
  Subtitle ≤ 18 words. Must make someone stop scrolling.
- Body slides: 4 or 5 of them. Each one covers ONE idea from the post.
  Title ≤ 8 words. Body ≤ 35 words. Never repeat the hook.
- CTA slide: lift any link from the body (github.com/... or example.com)
  and use it. If none, use github.com/<your-org>. Title ≤ 10 words.
  Subtitle ≤ 18 words.
- NO hype adjectives (seamless, robust, cutting-edge, etc.).
- NO marketing verbs (delivers, boosts, reduces without a number backing it).
- NO engagement-bait questions.

Output JSON only, no markdown fences:
{
  "hook":  {"title": "<≤12 words>", "subtitle": "<≤18 words>"},
  "body":  [{"title": "<≤8 words>", "body": "<≤35 words>"}, ...],
  "cta":   {"title": "<≤10 words>", "subtitle": "<≤18 words>", "link": "<url>"}
}
"""


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _slides_from_body(
    *, body: str, brand_id: str, cta_link: str, body_slides: int
) -> dict[str, Any]:
    cfg = brand_config(brand_id)
    voice = _load_file(cfg.get("voice_prompt_path"))

    system = (
        f"{voice}\n\n"
        f"---\n"
        f"{_BODY_TO_SLIDES_SYSTEM}\n"
        f"---\n"
        f"Produce exactly {body_slides} body slides. CTA link fallback: {cta_link}"
    )
    user = f"The post body:\n\n{body}\n\nRestructure into the JSON schema."

    s_ = settings()
    tier = "smart" if (s_.openai_api_key or s_.anthropic_api_key) else "cheap"
    mc = pick(tier)
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=4096,
        **mc.kwargs,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CarouselError(f"LLM returned invalid JSON: {exc} :: {raw[:200]!r}") from exc
    if "hook" not in data or "body" not in data or "cta" not in data:
        raise CarouselError(f"LLM output missing required keys: {list(data.keys())}")
    return data


async def generate_carousel_from_body(
    body: str,
    brand_id: str,
    *,
    body_slides: int = 5,
    cta_link: str = "github.com/<your-org>",
) -> pathlib.Path:
    """Generate a PDF carousel from a hand-written LinkedIn post body.

    Used by the sheet_posting pipeline: the post body becomes the LinkedIn
    description; the returned PDF is uploaded as the document attachment.
    """
    slide_data = await _slides_from_body(
        body=body,
        brand_id=brand_id,
        cta_link=cta_link,
        body_slides=body_slides,
    )
    # Delegate the rendering path via slide_data_override — reuse the same
    # fal.ai background gen + Pillow overlay + img2pdf compile we already have.
    return await generate_carousel(
        signal=None,
        brand_id=brand_id,
        body_slides=body_slides,
        cta_link=cta_link,
        slide_data_override=slide_data,
    )


# ---------------------------------------------------------------------------
# Background prompts — one Leonardo call per slide. Backgrounds are
# atmosphere only: composition, color, texture, depth. NO text, NO UI, NO
# letters. Pillow lays the actual headline/body/chrome on top.
# ---------------------------------------------------------------------------

def _build_background_prompt(
    *,
    role: str,                 # hook | body | cta
    archetype: str,            # hook|cta|split|stat|code|asymmetric|halo
    accent: str,
    base: str,
    secondary: str,
) -> str:
    """Atmospheric background prompt for Leonardo. Pure visual — no copy.

    Each archetype gets a slightly different mood so the deck reads as a
    designed set without being identical seven times. The archetype name
    matches the Pillow renderer's layout, so backgrounds visually support
    the foreground composition (e.g. the "stat" archetype gets a focal
    glow that complements a centered glyph).
    """
    base_style = (
        "dark editorial poster-grade abstract gradient artwork, deep black "
        f"base color {base}, soft accent neon green {accent} highlights, "
        f"accent electric blue {secondary} highlights, premium minimal "
        "atmospheric composition, smooth volumetric light, fine film grain, "
        "intentional negative space, designed not templated, Stripe Press "
        "meets a16z research report aesthetic"
    )
    role_mood = {
        "hook": "single strong off-center volumetric light glow, hero composition, bold atmospheric depth",
        "split": "subtle two-zone gradient with smoother top half, calmer palette so foreground text reads clearly",
        "stat": "soft radial glow centered, atmospheric depth field with concentric soft light bands",
        "code": "matte gray-on-black tonality, smooth charcoal gradient, terminal-aesthetic muted mood",
        "asymmetric": "directional energy pulling left-to-right, calm darker left side and gently active right edge",
        "halo": "soft concentric bands of light, meditative focused center, radial atmospheric depth",
        "cta": "upward volumetric light from below, soft perspective gradient, closing sign-off mood",
    }.get(archetype, "calm minimal atmospheric gradient, faint texture, room for foreground typography")

    return (
        f"{base_style}, {role_mood}. "
        "No literal patterns, no circuit boards, no schematics, no UI mockups. "
        "Pure abstract atmosphere — soft gradients, volumetric light, faint "
        "noise texture only. "
        "Absolutely no text, no letters, no words, no captions, no logos, "
        "no buttons, no people, no faces, no hands, no photographs, "
        "no clipart, no emoji."
    )


# ---------------------------------------------------------------------------
# Pillow renderer v2 — archetype-aware slide composition
#
# We render real typography on top of a Leonardo background. Each archetype
# has its own layout function that places the title, body, and at least one
# distinct graphic element (diagram, big glyph, terminal panel, dot column,
# halo rings, hero block, converging lines) so the deck reads as designed
# rather than identical seven times.
# ---------------------------------------------------------------------------

CONTENT_X = 90
CONTENT_W = SLIDE_W - 2 * CONTENT_X


def _render_slide_v2(
    *,
    background_path: pathlib.Path,
    archetype: str,                        # hook|cta|split|stat|code|asymmetric|halo
    slide_num: int,
    slide_total: int,
    title: str,
    body: str,
    link: str,
    accent: str,
    base: str,
    secondary: str = "#0088ff",
) -> Image.Image:
    """Compose one carousel slide. Background + darkening + chrome + archetype layout."""
    bg = Image.open(background_path).convert("RGBA")
    bg = _resize_cover(bg, SLIDE_W, SLIDE_H)

    # Darken for text readability. Hero slides keep more of the BG.
    darkness = 130 if archetype in ("hook", "cta") else 165
    overlay = Image.new("RGBA", (SLIDE_W, SLIDE_H), (0, 0, 0, darkness))
    bg = Image.alpha_composite(bg, overlay)

    # Soft vignette pulls the eye inward.
    vignette = Image.new("RGBA", (SLIDE_W, SLIDE_H), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    for i, alpha in enumerate([40, 30, 20, 10]):
        inset = (i + 1) * 20
        vdraw.rectangle(
            [(inset, inset), (SLIDE_W - inset, SLIDE_H - inset)],
            outline=(0, 0, 0, alpha), width=20,
        )
    bg = Image.alpha_composite(bg, vignette)

    draw = ImageDraw.Draw(bg)

    # ── Brand chrome (consistent on every slide) ───────────────────────────
    _draw_chrome(draw, slide_num, slide_total, accent, secondary)

    # ── Archetype layout ───────────────────────────────────────────────────
    if archetype == "hook":
        _layout_hook(draw, bg, title, body, accent, secondary)
    elif archetype == "cta":
        _layout_cta(draw, bg, title, body, link, accent, secondary)
    elif archetype == "split":
        _layout_split(draw, bg, title, body, accent)
    elif archetype == "stat":
        _layout_stat(draw, bg, title, body, accent)
    elif archetype == "code":
        _layout_code(draw, bg, title, body, accent)
    elif archetype == "asymmetric":
        _layout_asymmetric(draw, bg, title, body, accent)
    elif archetype == "halo":
        _layout_halo(draw, bg, title, body, accent)
    else:
        # Sane default — same as split
        _layout_split(draw, bg, title, body, accent)

    return bg.convert("RGB")


# ── Brand chrome ────────────────────────────────────────────────────────────

def _draw_chrome(
    draw: ImageDraw.ImageDraw,
    slide_num: int,
    slide_total: int,
    accent: str,
    secondary: str,
) -> None:
    """Top-left wordmark, top-right counter, bottom progress bar."""
    # Top-left: 4×36 accent bar + monospace wordmark
    draw.rectangle([(80, 80), (84, 116)], fill=accent)
    draw.text(
        (98, 88), "GLITCH · EXECUTOR",
        font=_font(_FONT_MONO, 20),
        fill=(255, 255, 255, 235),
    )
    # Top-right: 02 / 07
    counter = f"{slide_num:02d} / {slide_total:02d}"
    cf = _font(_FONT_MONO, 22)
    cw = draw.textlength(counter, font=cf)
    draw.text(
        (SLIDE_W - 80 - cw, 88), counter, font=cf,
        fill=_hex_to_rgba(secondary, 230),
    )
    # Bottom progress bar
    bar_y = SLIDE_H - 38
    bar_x0, bar_x1 = 80, SLIDE_W - 80
    draw.rectangle([(bar_x0, bar_y), (bar_x1, bar_y + 3)], fill=(70, 70, 75, 200))
    fill_w = int((bar_x1 - bar_x0) * (slide_num / slide_total))
    draw.rectangle([(bar_x0, bar_y), (bar_x0 + fill_w, bar_y + 3)], fill=accent)


# ── Layout: HOOK ────────────────────────────────────────────────────────────

def _layout_hook(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    accent: str,
    secondary: str,
) -> None:
    """Hero cover slide. OVERSIZED headline + accent bar + subtitle + footer + corner accent."""
    # Decorative corner accent (top-right): short diagonal segment + small dot.
    # Kept above the headline area so it never crosses copy.
    draw.line(
        [(SLIDE_W - 60, 160), (SLIDE_W - 200, 300)],
        fill=_hex_to_rgba(accent, 160), width=1,
    )
    draw.ellipse(
        [(SLIDE_W - 208, 292), (SLIDE_W - 192, 308)],
        fill=accent,
    )

    # Auto-shrink headline so it always fits 2-3 lines.
    title_font, title_lines = _autofit_title(title, max_size=92, min_size=58)
    body_font = _font(_FONT_REGULAR, 30)
    body_lines = _wrap_text(body, body_font, CONTENT_W - 80, draw)

    title_h = sum(title_font.size + 14 for _ in title_lines)
    body_h = sum(body_font.size + 8 for _ in body_lines)
    gap = 36
    total_h = title_h + 24 + 6 + 16 + gap + body_h  # title + gap + bar + gap + body
    y_top = (SLIDE_H - total_h) // 2

    y = y_top
    for line in title_lines:
        draw.text((CONTENT_X, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 14
    # Accent bar
    y += 24
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 200, y + 6)], fill=accent)
    y += 6 + 22
    for line in body_lines:
        draw.text((CONTENT_X, y), line, font=body_font, fill=(220, 220, 222, 255))
        y += body_font.size + 8

    _draw_footer_wordmark(draw, accent, secondary)


# ── Layout: CTA ─────────────────────────────────────────────────────────────

def _layout_cta(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    link: str,
    accent: str,
    secondary: str,
) -> None:
    """Closing slide: hero text + URL + decorative converging lines."""
    # Closing glyph top-right: three stacked horizontal bars decreasing
    gx, gy = SLIDE_W - 110, 150
    for i, w in enumerate([34, 22, 12]):
        draw.rectangle([(gx, gy + i * 8), (gx + w, gy + i * 8 + 3)], fill=accent)

    title_font, title_lines = _autofit_title(title, max_size=72, min_size=48)
    body_font = _font(_FONT_REGULAR, 28)
    body_lines = _wrap_text(body, body_font, CONTENT_W - 80, draw)

    title_h = sum(title_font.size + 12 for _ in title_lines)
    body_h = sum(body_font.size + 8 for _ in body_lines)
    link_h = 0
    link_font: ImageFont.FreeTypeFont | None = None
    if link:
        link_font = _fit_mono_to_width(link, CONTENT_W - 80, 26, min_size=18)
        link_h = link_font.size + 14
    # Anchor text to upper portion so converging lines decorate the empty
    # bottom half without crossing copy.
    y_top = 280
    text_block_end = y_top + title_h + 22 + 6 + 18 + body_h + (28 + link_h if link else 0)

    # Decorative converging lines from bottom-center fanning toward upper-mid
    # — but only in the empty space BELOW the text block.
    cx = SLIDE_W // 2
    line_top_y = max(text_block_end + 60, 900)
    for i, off in enumerate([-360, -210, -70, 70, 210, 360]):
        alpha = 35 if i in (0, 5) else 60
        draw.line(
            [(cx + off, SLIDE_H - 80), (cx + off // 6, line_top_y)],
            fill=_hex_to_rgba(accent, alpha), width=1,
        )

    y = y_top
    for line in title_lines:
        draw.text((CONTENT_X, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 12
    y += 22
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 180, y + 6)], fill=accent)
    y += 6 + 18
    for line in body_lines:
        draw.text((CONTENT_X, y), line, font=body_font, fill=(220, 220, 222, 255))
        y += body_font.size + 8
    if link and link_font is not None:
        y += 28
        draw.text((CONTENT_X, y), link, font=link_font, fill=accent)

    _draw_footer_wordmark(draw, accent, secondary)


# ── Layout: SPLIT (text top, 3-node diagram bottom) ─────────────────────────

def _layout_split(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    accent: str,
) -> None:
    """Top half: title + body. Bottom half: 3-node connected diagram."""
    title_font, title_lines = _autofit_title(title, max_size=64, min_size=44)
    body_font = _font(_FONT_REGULAR, 26)
    body_lines = _wrap_text(body, body_font, CONTENT_W - 60, draw)

    y = 260
    for line in title_lines:
        draw.text((CONTENT_X, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 10
    y += 18
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 130, y + 4)], fill=accent)
    y += 4 + 22
    for line in body_lines:
        draw.text((CONTENT_X, y), line, font=body_font, fill=(220, 220, 222, 255))
        y += body_font.size + 8

    # 3-node diagram in bottom 40% of frame
    diag_y = SLIDE_H - 320
    nodes = [(SLIDE_W // 2 - 320, diag_y), (SLIDE_W // 2, diag_y), (SLIDE_W // 2 + 320, diag_y)]
    for cx, cy in nodes:
        draw.ellipse([(cx - 32, cy - 32), (cx + 32, cy + 32)], outline=accent, width=2)
        draw.ellipse([(cx - 6, cy - 6), (cx + 6, cy + 6)], fill=accent)
    # Arrows between nodes
    for (x0, y0), (x1, _) in zip(nodes[:-1], nodes[1:]):
        draw.line([(x0 + 32, y0), (x1 - 38, y0)], fill=accent, width=2)
        # Arrow head
        draw.polygon(
            [(x1 - 38, y0 - 5), (x1 - 38, y0 + 5), (x1 - 32, y0)],
            fill=accent,
        )


# ── Layout: STAT (giant focal glyph centered, headline below) ───────────────

def _layout_stat(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    accent: str,
) -> None:
    """Big focal glyph (a stylized chevron) up top, headline + body underneath."""
    cx = SLIDE_W // 2
    glyph_y = 410
    # Stylized upward-chevron in accent + soft echoes
    for i, alpha in enumerate([255, 60, 30]):
        offset = i * 28
        draw.line(
            [(cx - 130 - offset, glyph_y + offset), (cx, glyph_y - 90 + offset)],
            fill=_hex_to_rgba(accent, alpha), width=8 if i == 0 else 4,
        )
        draw.line(
            [(cx + 130 + offset, glyph_y + offset), (cx, glyph_y - 90 + offset)],
            fill=_hex_to_rgba(accent, alpha), width=8 if i == 0 else 4,
        )

    title_font, title_lines = _autofit_title(title, max_size=58, min_size=40)
    body_font = _font(_FONT_REGULAR, 26)
    body_lines = _wrap_text(body, body_font, CONTENT_W - 60, draw)

    y = 720
    for line in title_lines:
        draw.text((CONTENT_X, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 10
    y += 18
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 130, y + 4)], fill=accent)
    y += 4 + 22
    for line in body_lines:
        draw.text((CONTENT_X, y), line, font=body_font, fill=(220, 220, 222, 255))
        y += body_font.size + 8


# ── Layout: CODE (terminal-panel framed text) ───────────────────────────────

def _layout_code(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    accent: str,
) -> None:
    """Centered terminal panel with title (bold) + body (mono) inside."""
    panel_x0, panel_x1 = 80, SLIDE_W - 80
    panel_y0, panel_y1 = 280, SLIDE_H - 220
    # Panel background
    draw.rounded_rectangle(
        [(panel_x0, panel_y0), (panel_x1, panel_y1)],
        radius=12, fill=(22, 22, 32, 245), outline=(60, 60, 70, 220), width=1,
    )
    # macOS-style header dots
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = panel_x0 + 28 + i * 22
        cy = panel_y0 + 22
        draw.ellipse([(cx - 7, cy - 7), (cx + 7, cy + 7)], fill=color)

    # Title + body inside panel
    inner_x = panel_x0 + 36
    inner_w = panel_x1 - panel_x0 - 72
    title_font, title_lines = _autofit_title(title, max_size=50, min_size=34, max_width=inner_w)
    body_font = _font(_FONT_MONO, 22)
    body_lines = _wrap_text(body, body_font, inner_w, draw)

    y = panel_y0 + 80
    for line in title_lines:
        draw.text((inner_x, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 10
    y += 14
    draw.rectangle([(inner_x, y), (inner_x + 120, y + 4)], fill=accent)
    y += 4 + 22
    for line in body_lines:
        draw.text((inner_x, y), line, font=body_font, fill=(210, 215, 220, 255))
        y += body_font.size + 8


# ── Layout: ASYMMETRIC (text left two-thirds, dot column right) ─────────────

def _layout_asymmetric(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    accent: str,
) -> None:
    """Headline + body anchored left 2/3 of frame; vertical dot indicator on right edge."""
    text_w = int(CONTENT_W * 0.65)
    title_font, title_lines = _autofit_title(title, max_size=60, min_size=40, max_width=text_w)
    body_font = _font(_FONT_REGULAR, 26)
    body_lines = _wrap_text(body, body_font, text_w, draw)

    title_h = sum(title_font.size + 10 for _ in title_lines)
    body_h = sum(body_font.size + 8 for _ in body_lines)
    total_h = title_h + 18 + 4 + 22 + body_h
    y_top = (SLIDE_H - total_h) // 2

    y = y_top
    for line in title_lines:
        draw.text((CONTENT_X, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 10
    y += 18
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 130, y + 4)], fill=accent)
    y += 4 + 22
    for line in body_lines:
        draw.text((CONTENT_X, y), line, font=body_font, fill=(220, 220, 222, 255))
        y += body_font.size + 8

    # Right-edge dot column — 7 small accent squares evenly distributed
    col_x = SLIDE_W - 130
    for i in range(7):
        cy = 280 + i * 110
        draw.rectangle([(col_x, cy), (col_x + 14, cy + 14)], fill=accent)


# ── Layout: HALO (concentric rings centered + headline below) ───────────────

def _layout_halo(
    draw: ImageDraw.ImageDraw,
    bg: Image.Image,
    title: str,
    body: str,
    accent: str,
) -> None:
    """Centered concentric rings + headline + body below the halo."""
    cx, cy = SLIDE_W // 2, 460
    # 4 concentric rings — outer faded, inner bright
    for i, (radius, alpha) in enumerate([(220, 30), (180, 60), (140, 110), (100, 255)]):
        bbox = [(cx - radius, cy - radius), (cx + radius, cy + radius)]
        draw.ellipse(bbox, outline=_hex_to_rgba(accent, alpha), width=2 if i < 3 else 3)

    # Headline + body BELOW the halo
    title_font, title_lines = _autofit_title(title, max_size=54, min_size=36)
    body_font = _font(_FONT_REGULAR, 26)
    body_lines = _wrap_text(body, body_font, CONTENT_W - 60, draw)

    y = 760
    for line in title_lines:
        draw.text((CONTENT_X, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += title_font.size + 10
    y += 18
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 130, y + 4)], fill=accent)
    y += 4 + 22
    for line in body_lines:
        draw.text((CONTENT_X, y), line, font=body_font, fill=(220, 220, 222, 255))
        y += body_font.size + 8


# ── Shared helpers ──────────────────────────────────────────────────────────

def _draw_footer_wordmark(
    draw: ImageDraw.ImageDraw, accent: str, secondary: str,
) -> None:
    """example.com + secondary pip near bottom — used on hook/cta only."""
    footer_font = _font(_FONT_MONO, 20)
    text = "example.com"
    fy = SLIDE_H - 90
    draw.text((CONTENT_X, fy), text, font=footer_font, fill=(200, 200, 200, 220))
    fw = draw.textlength(text, font=footer_font)
    pip_x = CONTENT_X + int(fw) + 14
    draw.rectangle(
        [(pip_x, fy + 2), (pip_x + 10, fy + 14)],
        fill=_hex_to_rgba(secondary, 255),
    )


def _autofit_title(
    text: str,
    *,
    max_size: int,
    min_size: int,
    max_width: int = CONTENT_W - 40,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Pick the largest title font where the text fits in ≤3 lines."""
    tmp = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(tmp)
    size = max_size
    while size > min_size:
        font = _font(_FONT_BOLD, size)
        lines = _wrap_text(text, font, max_width, d)
        if len(lines) <= 3:
            return font, lines
        size -= 4
    font = _font(_FONT_BOLD, min_size)
    return font, _wrap_text(text, font, max_width, d)


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Convert '#00ff88' → (0, 255, 136, alpha)."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def _fit_mono_to_width(
    text: str,
    max_width: int,
    start_size: int,
    min_size: int = 18,
) -> ImageFont.FreeTypeFont:
    """Shrink the mono font until `text` fits within max_width. Used for
    long URLs on the CTA slide that can't word-wrap."""
    # Rough pixel-per-char estimate avoids instantiating N fonts: bail fast
    # once the shrink is clearly unnecessary. For correctness we measure.
    tmp = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(tmp)
    size = start_size
    while size > min_size:
        font = _font(_FONT_MONO, size)
        if d.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(_FONT_MONO, min_size)


def _resize_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize + center-crop so the image fills (target_w, target_h) exactly."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Greedy word-wrap — respects explicit newlines the LLM included."""
    out_lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            out_lines.append("")
            continue
        words = paragraph.split()
        line = ""
        for w in words:
            candidate = f"{line} {w}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                if line:
                    out_lines.append(line)
                line = w
        if line:
            out_lines.append(line)
    return out_lines


def _text_block_height(lines: list[str], font, draw: ImageDraw.ImageDraw) -> int:
    return len(lines) * (font.size + 10)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _brand_colors(brand_id: str) -> tuple[str, str, str]:
    """Return (accent, base, secondary) from brand config. Secondary defaults
    to electric blue if the brand didn't declare one."""
    try:
        brand = brand_config(brand_id).get("brand", {})
    except KeyError:
        brand = {}
    return (
        brand.get("accent_color") or _DEFAULT_ACCENT,
        brand.get("base_color") or _DEFAULT_BASE,
        brand.get("secondary_color") or "#0088ff",
    )


def _load_file(path: str | None) -> str:
    if not path:
        return ""
    p = pathlib.Path(path)
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------------------
# img2pdf: compile PNG slides into a single PDF
# ---------------------------------------------------------------------------

def _compile_pdf(slide_paths: list[pathlib.Path], pdf_path: pathlib.Path) -> None:
    """Compile PNGs into a PDF. Upscales each slide to 2160x2700 with LANCZOS
    before stitching — gpt-image-2 outputs 1080x1350 which still shows mild
    softness on retina/mobile. 2x supersampling cleans up perceived sharpness
    of rendered text without re-running the model.
    """
    import io

    import img2pdf

    target_w, target_h = 2160, 2700
    raw_bytes: list[bytes] = []
    for p in slide_paths:
        with Image.open(p) as img:
            img = img.convert("RGB")
            if img.size != (target_w, target_h):
                img = img.resize((target_w, target_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=False)
            raw_bytes.append(buf.getvalue())
    with open(pdf_path, "wb") as fh:
        fh.write(img2pdf.convert(raw_bytes))
