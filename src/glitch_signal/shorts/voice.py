"""ElevenLabs TTS for the YouTube Shorts pipeline.

Single-take rendering: we concatenate hook + every segment's spoken
text + cta into one string with sentence-level pauses, then call
ElevenLabs once. Way cleaner audio (consistent prosody, no concat
artifacts) and cheaper than per-segment.

Returns the local path to the rendered mp3.
"""
from __future__ import annotations

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


class VoiceGenError(RuntimeError):
    """Raised on a non-recoverable ElevenLabs failure."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPError, VoiceGenError)),
)
async def render_voiceover(
    *,
    brand_id: str,
    script: dict,
    out_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Render the full script as one ElevenLabs mp3."""
    s = settings()
    api_key = s.elevenlabs_api_key
    if not api_key:
        raise VoiceGenError("ELEVENLABS_API_KEY not set")
    voice_id = s.elevenlabs_voice_id
    model_id = s.elevenlabs_model

    out_dir = out_dir or (
        pathlib.Path(s.video_storage_path) / "shorts" / brand_id / "audio"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex}.mp3"

    text = _stitch(script)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.45,           # leave room for natural inflection
            "similarity_boost": 0.75,
            "style": 0.10,               # subtle emotion
            "use_speaker_boost": True,
        },
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            url,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        raise VoiceGenError(
            f"ElevenLabs {resp.status_code}: {resp.text[:300]}"
        )
    out_path.write_bytes(resp.content)
    log.info(
        "shorts.voice.done",
        brand_id=brand_id, voice_id=voice_id, model_id=model_id,
        path=str(out_path), size_kb=out_path.stat().st_size // 1024,
        text_chars=len(text),
    )
    return out_path


def _stitch(script: dict) -> str:
    """Combine hook + segments + cta into one continuous narration.
    Periods give ElevenLabs natural breaths; explicit pauses are added
    between major beats with `<break time="500ms"/>` tags ElevenLabs
    supports.
    """
    parts: list[str] = []
    if hook := (script.get("hook") or "").strip():
        parts.append(_clean(hook))
    for seg in script.get("segments") or []:
        spoken = (seg.get("spoken") or "").strip()
        if spoken:
            parts.append(_clean(spoken))
    if cta := (script.get("cta") or "").strip():
        parts.append(_clean(cta))
    return ' <break time="500ms"/> '.join(parts)


def _clean(s: str) -> str:
    """Strip markdown and collapse whitespace; ElevenLabs reads asterisks
    as 'asterisk' verbatim if you don't."""
    s = s.replace("**", "").replace("*", "").replace("_", " ")
    s = " ".join(s.split())
    return s.strip()
