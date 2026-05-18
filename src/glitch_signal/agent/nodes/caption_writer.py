"""CaptionWriter node — LLM-generates title + caption + hashtags for a Signal.

Runs after drive_scout in the drive_footage pipeline. Writes a ContentScript
(script_body = caption) and a VideoAsset pointing at the already-downloaded
local file — bypassing storyboard / video generation / assembler entirely.

Voice guide: brand config's `voice_prompt_path` (a markdown file, gitignored)
provides the per-brand style. Falls back to a neutral default when absent.

Two captioning modes, switchable per brand via
`caption_writer.mode` in the brand config:

- `filename` (default) — captions from the Drive file name only. Cheap
  (Gemini Flash, fractions of a cent per caption) but generic.
- `vision` — uploads the actual video to Gemini 2.5 Pro and writes the
  caption grounded in on-screen content. Roughly $0.02-0.05 per 30s clip
  and 10-30s of latency. Dramatically better specificity — the difference
  between "another ayurvedic oil post" and "hair massage demo with
  brahmi-infused oil on a wooden comb". On failure, falls back to
  filename mode so the pipeline never stalls on vendor hiccups.
"""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import UTC, datetime

import litellm
import structlog

from glitch_signal.agent.llm import pick
from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import ContentScript, Signal, VideoAsset
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

_ASSEMBLER_TAG = "drive_passthrough@1.0"   # marks assets that skipped assembler

_DEFAULT_VOICE = (
    "Warm, grounded, honest. No hype, no superlatives, no engagement bait. "
    "Write like a real person talking to one person, not a brand broadcasting."
)

_SYSTEM_TEMPLATE = """You are writing TikTok captions for a brand.

BRAND: {display_name}
VOICE RULES:
{voice}

CONTEXT: The video has already been shot and edited. You are writing
*around* the video, not describing it frame-by-frame. Keep the viewer
watching and wanting to engage.

CAPTION RULES:
- Total length ≤ 2000 characters.
- Start with a hook in the first 80 characters — this is what shows before
  the "more" cutoff in the TikTok feed.
- End with 3–8 hashtags, space-separated, all lowercase, no punctuation.
- No emoji walls. At most 2 emojis in the whole caption.
- Never fabricate product claims, medical claims, or results.

Return JSON ONLY:
{{
  "title": "plain text, ≤ 100 chars, used for TikTok title field",
  "caption": "full caption text including hashtags at the end",
  "hashtags": ["tag1", "tag2", ...]
}}
"""


async def caption_writer_node(state: SignalAgentState) -> SignalAgentState:
    signal_id = state.get("signal_id")
    if not signal_id:
        return {**state, "error": "caption_writer: missing signal_id"}

    brand_id = state.get("brand_id") or settings().default_brand_id
    platform = state.get("platform") or "tiktok"

    factory = _session_factory()
    async with factory() as session:
        signal = await session.get(Signal, signal_id)
        if not signal:
            return {**state, "error": f"caption_writer: Signal {signal_id} not found"}

        # The local path was stashed by drive_scout in the signals list; if
        # this node is re-run standalone (e.g. caption regeneration), fall back
        # to the conventional location.
        local_path = _resolve_local_path(state, signal, brand_id)

        # Prefer a human-written caption from the brand's tracker sheet when
        # present. Operators often pre-write captions in the `caption` column
        # alongside the video row — that hand-authored text should win over
        # LLM output. Fall through to LLM generation when the sheet has no
        # caption (or no sheet is configured).
        sheet_caption = await _read_sheet_caption(brand_id, signal)
        if sheet_caption:
            caption = sheet_caption
            # Derive a title the same way _read_caption does for publishing,
            # and harvest hashtags from the caption body.
            first = caption.split(".")[0][:100].strip()
            title = first or caption[:100].strip()
            hashtags = [
                tok[1:].rstrip(".,!?").lower()
                for tok in caption.split()
                if tok.startswith("#") and len(tok) > 1
            ]
            log.info(
                "caption_writer.used_sheet_caption",
                brand_id=brand_id,
                signal_id=signal_id,
                caption_preview=caption[:80],
            )
        else:
            title, caption, hashtags = await _generate_caption(
                signal, brand_id, platform, local_path=local_path
            )

        script_id = str(uuid.uuid4())
        asset_id = str(uuid.uuid4())
        now = datetime.now(UTC).replace(tzinfo=None)

        cs = ContentScript(
            id=script_id,
            brand_id=brand_id,
            signal_id=signal_id,
            platform=platform,
            script_body=caption,
            content_type="drive",
            key_visuals=json.dumps([]),
            shots="[]",
            status="done",
            created_at=now,
        )
        session.add(cs)

        asset = VideoAsset(
            id=asset_id,
            brand_id=brand_id,
            script_id=script_id,
            file_path=str(local_path),
            duration_s=await _probe_duration(local_path),
            assembler_version=_ASSEMBLER_TAG,
            created_at=now,
        )
        session.add(asset)

        signal.status = "scripted"
        session.add(signal)
        await session.commit()

    # Push caption + "captioned" status to the brand's tracker sheet
    # (no-op when no sheet is configured on the brand). When the caption
    # came FROM the sheet, we only bump status — we never clobber a human
    # caption with the same string we just read.
    from glitch_signal.integrations import sheet_tracker
    if signal.source == "drive":
        video_name = signal.summary.replace("Drive clip: ", "", 1)
        updates: dict = {"status": "captioned"}
        if not sheet_caption:
            updates["caption"] = caption
        await sheet_tracker.update_by_video_name(brand_id, video_name, updates)

    log.info(
        "caption_writer.done",
        brand_id=brand_id,
        signal_id=signal_id,
        script_id=script_id,
        asset_id=asset_id,
        title=title[:60],
        n_hashtags=len(hashtags),
    )

    return {
        **state,
        "brand_id": brand_id,
        "script_id": script_id,
        "script_body": caption,
        "content_type": "drive",
        "key_visuals": [],
        "asset_id": asset_id,
        "asset_path": str(local_path),
    }


async def _read_sheet_caption(brand_id: str, signal: Signal) -> str | None:
    """Return a pre-written caption from the brand's tracker sheet, or None.

    Only applicable to drive-source signals — we key on the Drive filename.
    Returns None when the sheet isn't configured, the row is missing, or the
    `caption` cell is blank. Failures never raise — caption generation is
    the fallback.
    """
    if signal.source != "drive":
        return None
    try:
        from glitch_signal.integrations import sheet_tracker
        video_name = signal.summary.replace("Drive clip: ", "", 1)
        row = await sheet_tracker.read_by_video_name(brand_id, video_name)
    except Exception as exc:
        log.warning(
            "caption_writer.sheet_read_failed",
            brand_id=brand_id, signal_id=signal.id, error=str(exc)[:200],
        )
        return None
    if not row:
        return None
    caption = (row.get("caption") or "").strip()
    return caption or None


async def _generate_caption(
    signal: Signal, brand_id: str, platform: str,
    local_path: pathlib.Path | None = None,
) -> tuple[str, str, list[str]]:
    cfg = brand_config(brand_id)
    display_name = cfg.get("display_name", brand_id)
    voice = _load_voice(cfg) or _DEFAULT_VOICE
    default_hashtags: list[str] = cfg.get("default_hashtags") or []
    cw_cfg = (cfg.get("caption_writer") or {})
    mode = cw_cfg.get("mode", "filename")

    system_prompt = _SYSTEM_TEMPLATE.format(display_name=display_name, voice=voice)
    user_context = (
        f"Platform: {platform}\n"
        f"Drive clip filename: {signal.summary}\n"
        f"Default hashtags to consider: {', '.join(default_hashtags) or '(none)'}\n\n"
        "Write the post."
    )

    data: dict = {}
    if mode == "vision" and local_path is not None and local_path.exists():
        try:
            data = await _generate_via_vision(
                local_path=local_path,
                system_prompt=system_prompt,
                user_context=user_context,
                model_override=cw_cfg.get("vision_model"),
            )
        except Exception as exc:
            log.warning(
                "caption_writer.vision_failed",
                brand_id=brand_id,
                signal_id=signal.id,
                error=str(exc)[:300],
            )
            if not cw_cfg.get("vision_fallback_to_filename", True):
                raise
            data = {}
        else:
            log.info(
                "caption_writer.vision_ok",
                brand_id=brand_id,
                signal_id=signal.id,
                bytes=local_path.stat().st_size if local_path.exists() else 0,
            )
    elif mode == "rules_based":
        data = await _generate_via_rules_based(
            signal=signal,
            system_prompt=system_prompt,
            user_context=user_context,
            catalog_path=cw_cfg.get("product_catalog_path"),
        )

    if not data:
        data = await _generate_via_filename(
            system_prompt=system_prompt, user_context=user_context
        )

    title = str(data.get("title", "")).strip()[:100] or display_name
    caption = str(data.get("caption", "")).strip()[:2000]
    raw_tags = data.get("hashtags") or []
    hashtags = [str(t).lstrip("#").strip().lower() for t in raw_tags if t]

    # Fail-soft fallback: if both LLM paths didn't yield a caption, compose
    # one from the brand's default_hashtags (stripping "#" then re-adding
    # so the caption body is correctly prefixed).
    if not caption:
        fallback_tags = hashtags or [
            h.lstrip("#").strip().lower() for h in default_hashtags if h
        ]
        hashtag_block = " ".join(f"#{t}" for t in fallback_tags)
        caption = (f"{display_name}" + (f"\n\n{hashtag_block}" if hashtag_block else "")).strip()
        hashtags = fallback_tags

    return title, caption, hashtags


async def _acompletion_with_retry(**kwargs) -> object:
    """Call litellm.acompletion with exponential backoff on transient errors.

    Gemini (our `cheap` tier) routinely returns 503 ServiceUnavailable
    during high-demand windows — observed during a drive-footage preview run
    on 2026-04-17 where consecutive caption calls hit 503 over a span
    of ~2 minutes. A single try would fall straight back to the caption
    writer's fail-soft template; with backoff the first attempt after
    the first 5xx usually succeeds.

    Retries on litellm's transient-error family (ServiceUnavailable,
    RateLimit, InternalServerError, BadGateway, APIConnection,
    InternalServerError). Hard errors (Auth, BadRequest, NotFound,
    ContextWindowExceeded) are re-raised immediately — no retry.

    Backoff: 30s → 60s → 120s (5 attempts total). Caller sees the
    original exception if every attempt fails.
    """
    import asyncio as _asyncio

    transient = (
        litellm.ServiceUnavailableError,
        litellm.RateLimitError,
        litellm.InternalServerError,
        litellm.BadGatewayError,
        litellm.APIConnectionError,
    )
    max_attempts = 5
    base_delay_s = 30

    for attempt in range(max_attempts):
        try:
            return await litellm.acompletion(**kwargs)
        except transient as exc:
            if attempt == max_attempts - 1:
                raise
            delay = min(base_delay_s * (2 ** attempt), 120)
            log.info(
                "caption_writer.retry_transient",
                attempt=attempt + 1,
                delay_s=delay,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            await _asyncio.sleep(delay)
    # Unreachable — loop either returns or raises.
    raise RuntimeError("_acompletion_with_retry: exited loop without outcome")


async def _generate_via_rules_based(
    *,
    signal: Signal,
    system_prompt: str,
    user_context: str,
    catalog_path: str | None,
) -> dict:
    """Rules-based caption: parse filename + inject brand catalog into the prompt.

    Cheap — same Gemini Flash tier as filename mode — but the LLM is
    handed a structured parse of the filename (product, ad_num, geo,
    variant tags) and the brand's product catalog. Output stays on-brand
    and avoids the regulatory landmines a free-text prompt can trip.
    """
    from glitch_signal.media.filename_parser import parse as parse_filename

    filename = signal.summary.replace("Drive clip: ", "", 1)
    parsed = parse_filename(filename)

    catalog_text = ""
    if catalog_path:
        p = pathlib.Path(catalog_path)
        if not p.is_absolute():
            p = pathlib.Path.cwd() / catalog_path
        if p.exists():
            catalog_text = p.read_text().strip()
        else:
            log.warning("caption_writer.catalog_missing", path=str(p))

    parsed_block = (
        f"Parsed filename:\n"
        f"  product: {parsed.product or '(unparsed — use generic Ayurvedic framing)'}\n"
        f"  ad_num:  {parsed.ad_num if parsed.ad_num is not None else '(n/a)'}\n"
        f"  geo:     {parsed.geo or '(n/a)'}\n"
        f"  variant_tags: {list(parsed.variant_tags) or '(none)'}\n"
        f"  variant_group: {parsed.variant_group}\n"
    )

    full_user = (
        f"{user_context}\n\n"
        f"{parsed_block}\n"
        + (f"Brand catalog + rules:\n---\n{catalog_text}\n---\n\n" if catalog_text else "")
        + "Write the post using the parsed fields + catalog. Follow the hard "
          "rules in the catalog exactly — never claim cure/treat/prevent, "
          "never invent results. If the parsed product has no direct SKU, "
          "use the fallback framing listed in the catalog."
    )

    mc = pick("cheap")
    raw_content = ""
    try:
        resp = await _acompletion_with_retry(
            model=mc.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_user},
            ],
            response_format={"type": "json_object"},
            max_tokens=4096,
            **mc.kwargs,
        )
        raw_content = resp.choices[0].message.content or ""
        return _parse_caption_json(raw_content)
    except Exception as exc:
        log.warning(
            "caption_writer.rules_based_failed",
            error=str(exc),
            raw_preview=raw_content[:200] if raw_content else "",
            filename=filename,
        )
        return {}


async def _generate_via_filename(*, system_prompt: str, user_context: str) -> dict:
    """Filename-only caption path — cheap, text-only, default."""
    # DISPATCH_MODE gates PUBLISH actions (posting to TikTok, sending emails,
    # etc.), NOT every LLM call. Caption generation is cheap, text-only,
    # and exactly what the operator needs to review during dry-run —
    # skipping it leaves them previewing template fallback captions that
    # don't reflect the real system behaviour.
    mc = pick("cheap")
    raw_content = ""
    try:
        resp = await _acompletion_with_retry(
            model=mc.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_context},
            ],
            response_format={"type": "json_object"},
            # Gemini 2.5 Flash counts reasoning ("thinking") tokens against
            # max_tokens and will silently truncate the visible output when
            # the ceiling is tight. 4096 comfortably covers a 2000-char
            # caption plus whatever thinking the model wants to do.
            max_tokens=4096,
            **mc.kwargs,
        )
        raw_content = resp.choices[0].message.content or ""
        return _parse_caption_json(raw_content)
    except Exception as exc:
        log.warning(
            "caption_writer.llm_failed",
            error=str(exc),
            raw_preview=raw_content[:200] if raw_content else "",
        )
        return {}


async def _generate_via_vision(
    *,
    local_path: pathlib.Path,
    system_prompt: str,
    user_context: str,
    model_override: str | None = None,
) -> dict:
    """Upload the video to Gemini and caption from on-screen content.

    Uses google-genai's File API so we're not limited by the inline-byte
    cap. Files auto-expire after 48h on Gemini's side and we call
    `delete` after use so we don't pile up stale uploads.
    """
    import asyncio as _asyncio

    from google import genai
    from google.genai import types

    s = settings()
    if not s.google_api_key:
        raise RuntimeError("caption_writer.vision: GOOGLE_API_KEY is not set")

    model = model_override or "gemini-2.5-pro"

    def _sync_call() -> dict:
        client = genai.Client(api_key=s.google_api_key)
        uploaded = client.files.upload(file=str(local_path))
        try:
            # File API processes the video asynchronously — wait for ACTIVE.
            import time as _time
            deadline = _time.time() + 120   # 2 min ceiling
            while getattr(uploaded, "state", None) and str(uploaded.state).endswith("PROCESSING"):
                if _time.time() > deadline:
                    raise RuntimeError("caption_writer.vision: File API processing timed out")
                _time.sleep(2)
                uploaded = client.files.get(name=uploaded.name)
            if str(getattr(uploaded, "state", "")).endswith("FAILED"):
                raise RuntimeError(
                    f"caption_writer.vision: File API processing failed for {local_path.name}"
                )

            resp = client.models.generate_content(
                model=model,
                contents=[uploaded, system_prompt + "\n\n" + user_context],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            raw = getattr(resp, "text", "") or ""
            return _parse_caption_json(raw)
        finally:
            try:
                client.files.delete(name=uploaded.name)
            except Exception as exc:
                log.warning(
                    "caption_writer.vision_cleanup_failed",
                    file_name=getattr(uploaded, "name", "<unknown>"),
                    error=str(exc)[:200],
                )

    return await _asyncio.to_thread(_sync_call)


def _parse_caption_json(raw: str) -> dict:
    """Best-effort JSON parse for LLM output.

    Handles the common failure modes we've seen in practice:
    - leading/trailing whitespace or markdown fences (```json ... ```)
    - output that ended mid-generation (truncated) — try to recover the
      last valid {"title": ..., "caption": ..., "hashtags": [...]} block
    """
    if not raw:
        return {}
    text = raw.strip()

    # Strip markdown code fences if the model ignored response_format=json.
    if text.startswith("```"):
        # Drop the leading ```[json] and the trailing ```
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Happy path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Recovery: find the last closing brace that still yields valid JSON.
    # This rescues outputs truncated after "caption": "..." but before the
    # closing brace.
    last_brace = text.rfind("}")
    while last_brace > 0:
        try:
            return json.loads(text[: last_brace + 1])
        except json.JSONDecodeError:
            last_brace = text.rfind("}", 0, last_brace)
    return {}


def _load_voice(cfg: dict) -> str | None:
    rel = cfg.get("voice_prompt_path")
    if not rel:
        return None
    path = pathlib.Path(rel)
    if not path.is_absolute():
        # Resolve relative to the repo root (CWD of the service).
        path = pathlib.Path.cwd() / rel
    if not path.exists():
        log.warning("caption_writer.voice_prompt_missing", path=str(path))
        return None
    return path.read_text().strip()


def _resolve_local_path(
    state: SignalAgentState, signal: Signal, brand_id: str
) -> pathlib.Path:
    # Prefer what drive_scout passed through in state.
    for entry in state.get("signals") or []:
        if entry.get("id") == signal.id and entry.get("local_path"):
            return pathlib.Path(entry["local_path"])
    # Fallback: conventional location (drive_scout's download target).
    return (
        pathlib.Path(settings().video_storage_path)
        / "drive"
        / brand_id
        / f"{signal.source_ref}.mp4"
    )


async def _probe_duration(path: pathlib.Path) -> float:
    if not path.exists():
        return 0.0
    try:
        import ffmpeg
        probe = ffmpeg.probe(str(path))
        return float(probe["format"].get("duration", 0.0))
    except Exception:
        return 0.0
