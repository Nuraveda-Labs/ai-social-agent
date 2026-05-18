"""Quote-card rendering: one sheet body → one designed square image →
upload as a single LinkedIn/X image post.

Quote cards work where a short, punchy angle needs visual impact — stat
reveals, one-line takeaways, milestone callouts. Unlike carousel rows
(multi-slide deck) and text rows (plain tweet), a quote card is ONE image
with the key phrase displayed prominently.

Pipeline (May 2026 rebuild — code-rendered text, AI background only):
    1. LLM condenses the sheet body into:
         - headline       ≤ 10 words (the hook line)
         - subline        ≤ 15 words (optional support)
         - link           (pulled from body if present, else org URL)
    2. Leonardo generates a poster-grade abstract BACKGROUND (no text in
       the prompt — text/letters/UI explicitly negative-prompted).
    3. Pillow draws the brand chrome, the headline, the accent bar, the
       subline, and the link/pip on top of the background.
    4. Caller uploads the PNG via Upload-Post upload_photos() with the
       original sheet body as the post caption.

Why this shape: image models are not layout engines. Asking them to render
real copy at 10–18pt produces drift, mis-spellings, and inconsistent
margins. Code-driven typography on top of an AI background gives Canva-Pro
quality with first-try reliability.
"""
from __future__ import annotations

import json
import pathlib
import uuid

import litellm
import structlog
from PIL import Image, ImageDraw, ImageFont
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.agent.llm import pick
from glitch_signal.config import brand_config, settings
from glitch_signal.media.image_gen import generate_background

log = structlog.get_logger(__name__)


CARD_W = 1080
CARD_H = 1080
CONTENT_X = 90
CONTENT_W = CARD_W - 2 * CONTENT_X

_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
_FONT_REGULAR = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
_FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

_DEFAULT_ACCENT = "#00ff88"
_DEFAULT_BASE = "#0a0a0f"
_DEFAULT_SECONDARY = "#0088ff"


_QUOTE_SYSTEM = """You distill a polished social-media post into the text for a
single designed image (a "quote card" for LinkedIn or X).

The image will display the headline, subline, and link as the main visual
content — so each piece must be short, impactful, and stand alone.

Rules:
- Voice stays identical to the input body; do not rewrite the tone.
- Do not fabricate claims. If a number appears in the body, you may use it.
  If no number is there, don't invent one.
- Do not use marketing verbs ("reduces", "delivers", "boosts").
- No hype adjectives (seamless, robust, game-changing, cutting-edge).
- Headline must be ≤ 10 words. Sharpest possible phrasing. Opens the thought.
- Subline must be ≤ 15 words. Closes the thought or adds the "why it matters".
- Link: lift any GitHub / domain URL from the body. If none, use the default provided.

Output ONLY valid JSON matching this exact schema — no markdown fences:
{
  "headline": "<≤10 words>",
  "subline":  "<≤15 words>",
  "link":     "<url or empty string>"
}
"""


class QuoteCardError(RuntimeError):
    pass


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _distill_body(
    *, body: str, brand_id: str, default_link: str,
) -> dict[str, str]:
    """LLM call: polished post body → {headline, subline, link} for the card."""
    cfg = brand_config(brand_id)
    voice_path = cfg.get("voice_prompt_path")
    voice = ""
    if voice_path:
        p = pathlib.Path(voice_path)
        if p.exists():
            voice = p.read_text()

    system = (
        f"{voice}\n\n---\n{_QUOTE_SYSTEM}\n---\n"
        f"Default link if the body has no URL: {default_link}"
    )
    user = f"The body:\n\n{body}\n\nReturn the JSON."

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
        max_tokens=1024,
        **mc.kwargs,
    )
    raw = (resp.choices[0].message.content or "").strip() or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QuoteCardError(f"LLM returned invalid JSON: {exc} :: {raw[:200]!r}") from exc
    return {
        "headline": str(data.get("headline", "")).strip(),
        "subline": str(data.get("subline", "")).strip(),
        "link": str(data.get("link", "")).strip() or default_link,
    }


def _brand_colors(brand_id: str) -> tuple[str, str, str]:
    """Return (accent, base, secondary) from brand config."""
    try:
        brand = brand_config(brand_id).get("brand", {})
    except KeyError:
        brand = {}
    return (
        brand.get("accent_color") or _DEFAULT_ACCENT,
        brand.get("base_color") or _DEFAULT_BASE,
        brand.get("secondary_color") or _DEFAULT_SECONDARY,
    )


def _wordmark(brand_id: str) -> str:
    return "GLITCH · EXECUTOR" if brand_id == "glitch_executor" else "TEJAS · GLITCH"


# ── Background prompt (Leonardo) ────────────────────────────────────────────

def _build_background_prompt(*, accent: str, base: str, secondary: str) -> str:
    """Atmosphere only — no text, no UI. Pillow places the typography."""
    return (
        "dark editorial poster-grade abstract gradient artwork, deep black "
        f"base color {base}, soft accent neon green {accent} highlights, "
        f"accent electric blue {secondary} highlights, premium minimal "
        "atmospheric composition, single strong off-center volumetric light "
        "glow, smooth depth, fine film grain, intentional negative space, "
        "designed not templated, Stripe Press aesthetic. "
        "No literal patterns, no circuit boards, no schematics, no UI. "
        "Pure abstract atmosphere — soft gradients, volumetric light, faint "
        "noise texture only. "
        "Absolutely no text, no letters, no words, no captions, no logos, "
        "no buttons, no people, no faces, no hands, no photographs."
    )


# ── Pillow renderer ─────────────────────────────────────────────────────────

def _render_card(
    *,
    background_path: pathlib.Path,
    headline: str,
    subline: str,
    link: str,
    brand_id: str,
    accent: str,
    base: str,
    secondary: str,
) -> Image.Image:
    """Compose a 1080×1080 quote card: background + chrome + text."""
    bg = Image.open(background_path).convert("RGBA")
    bg = _resize_cover(bg, CARD_W, CARD_H)

    # Darken for text readability
    overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 140))
    bg = Image.alpha_composite(bg, overlay)

    # Soft vignette
    vignette = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    for i, alpha in enumerate([40, 30, 20, 10]):
        inset = (i + 1) * 20
        vdraw.rectangle(
            [(inset, inset), (CARD_W - inset, CARD_H - inset)],
            outline=(0, 0, 0, alpha), width=20,
        )
    bg = Image.alpha_composite(bg, vignette)

    draw = ImageDraw.Draw(bg)

    # ── Brand chrome ────────────────────────────────────────────────
    # Top-left: 4×36 accent bar + monospace wordmark
    draw.rectangle([(80, 70), (84, 106)], fill=accent)
    draw.text(
        (98, 78), _wordmark(brand_id),
        font=_font(_FONT_MONO, 20),
        fill=(255, 255, 255, 235),
    )
    # Top-right: brand-name marker in secondary
    right_text = "GLITCH EXECUTOR"
    rf = _font(_FONT_MONO, 18)
    rw = draw.textlength(right_text, font=rf)
    draw.text(
        (CARD_W - 80 - rw, 80), right_text, font=rf,
        fill=_hex_to_rgba(secondary, 220),
    )

    # ── Decorative diagonal accent (top-right corner) ───────────────
    draw.line(
        [(CARD_W - 60, 200), (CARD_W - 320, 440)],
        fill=_hex_to_rgba(accent, 180), width=1,
    )
    draw.ellipse(
        [(CARD_W - 328, 432), (CARD_W - 312, 448)],
        fill=accent,
    )

    # ── Headline + bar + subline (centered vertically) ──────────────
    headline_font, headline_lines = _autofit_title(
        headline, max_size=82, min_size=52, max_width=CONTENT_W - 40,
    )
    subline_font = _font(_FONT_REGULAR, 30)
    subline_lines = _wrap_text(subline, subline_font, CONTENT_W - 40, draw) if subline else []

    headline_h = sum(headline_font.size + 14 for _ in headline_lines)
    subline_h = sum(subline_font.size + 8 for _ in subline_lines)
    bar_block_h = 24 + 6 + 22  # gap + bar + gap
    total_h = headline_h + bar_block_h + subline_h
    y_top = (CARD_H - total_h) // 2

    y = y_top
    for line in headline_lines:
        draw.text((CONTENT_X, y), line, font=headline_font, fill=(255, 255, 255, 255))
        y += headline_font.size + 14
    y += 24
    draw.rectangle([(CONTENT_X, y), (CONTENT_X + 200, y + 6)], fill=accent)
    y += 6 + 22
    for line in subline_lines:
        draw.text((CONTENT_X, y), line, font=subline_font, fill=(220, 220, 222, 255))
        y += subline_font.size + 8

    # ── Footer: link + secondary pip ────────────────────────────────
    if link:
        link_font = _fit_mono_to_width(link, CONTENT_W, 24, min_size=16)
        ly = CARD_H - 90
        draw.text((CONTENT_X, ly), link, font=link_font, fill=accent)
        lw = draw.textlength(link, font=link_font)
        pip_x = CONTENT_X + int(lw) + 14
        draw.rectangle(
            [(pip_x, ly + 4), (pip_x + 10, ly + 14)],
            fill=_hex_to_rgba(secondary, 255),
        )

    return bg.convert("RGB")


# ── Public entry point ──────────────────────────────────────────────────────

async def generate_quote_card(
    body: str,
    brand_id: str,
    *,
    default_link: str = "github.com/glitch-exec-labs",
    quality: str = "high",  # kept for API compat — no longer affects render path
) -> pathlib.Path:
    """End-to-end: body → distilled JSON → Leonardo background → Pillow render → PNG path."""
    s = settings()
    if s.is_dry_run:
        fake = pathlib.Path(f"/tmp/dry-run-quote-card-{uuid.uuid4().hex[:8]}.png")
        log.info("quote_card.dry_run", brand_id=brand_id, path=str(fake))
        return fake

    distilled = await _distill_body(
        body=body, brand_id=brand_id, default_link=default_link,
    )
    accent, base, secondary = _brand_colors(brand_id)

    bg_path = await generate_background(
        prompt=_build_background_prompt(accent=accent, base=base, secondary=secondary),
        brand_id=brand_id,
        aspect="1:1",
    )

    card = _render_card(
        background_path=bg_path,
        headline=distilled["headline"],
        subline=distilled["subline"],
        link=distilled["link"],
        brand_id=brand_id,
        accent=accent, base=base, secondary=secondary,
    )

    out_dir = pathlib.Path(s.video_storage_path) / "images" / brand_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"quote_{uuid.uuid4().hex}.png"
    card.save(out_path, format="PNG", optimize=False)

    log.info(
        "quote_card.done",
        brand_id=brand_id,
        path=str(out_path),
        headline=distilled["headline"],
        size_kb=out_path.stat().st_size // 1024,
    )
    return out_path


# ── Pillow helpers (local copy — keeps quote_card self-contained) ──────────

def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def _resize_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    out: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        words = paragraph.split()
        line = ""
        for w in words:
            cand = f"{line} {w}".strip()
            if draw.textlength(cand, font=font) <= max_width:
                line = cand
            else:
                if line:
                    out.append(line)
                line = w
        if line:
            out.append(line)
    return out


def _autofit_title(
    text: str, *, max_size: int, min_size: int, max_width: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
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


def _fit_mono_to_width(
    text: str, max_width: int, start_size: int, min_size: int = 16,
) -> ImageFont.FreeTypeFont:
    tmp = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(tmp)
    size = start_size
    while size > min_size:
        font = _font(_FONT_MONO, size)
        if d.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(_FONT_MONO, min_size)
