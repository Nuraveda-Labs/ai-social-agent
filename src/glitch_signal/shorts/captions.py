"""Word-level burned captions for the Shorts pipeline.

Whisper transcribes the ElevenLabs voiceover with word-level timestamps,
and we render an .ass subtitle file ffmpeg's `subtitles=` filter can
burn in. Word-level (not sentence-level) is the modern Shorts/TikTok
norm — keeps the eye locked on the screen, boosts watch time.

Uses OpenAI's Whisper API (cheap, ~$0.006/min — at 45s ≈ $0.005/short).
"""
from __future__ import annotations

import json
import pathlib

import httpx
import structlog

from glitch_signal.config import settings

log = structlog.get_logger(__name__)


async def transcribe_words(audio_path: pathlib.Path) -> list[dict]:
    """Whisper API → list of {word, start, end} dicts."""
    s = settings()
    if not s.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    with open(audio_path, "rb") as f:
        files = {
            "file": (audio_path.name, f.read(), "audio/mpeg"),
        }
    data = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {s.openai_api_key}"},
            files=files,
            data=data,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Whisper API {r.status_code}: {r.text[:300]}")
    payload = r.json()
    words = payload.get("words") or []
    log.info(
        "shorts.captions.transcribed",
        word_count=len(words),
        duration_s=payload.get("duration"),
    )
    return [
        {"word": w.get("word", "").strip(),
         "start": float(w.get("start", 0)),
         "end": float(w.get("end", 0))}
        for w in words if w.get("word")
    ]


def build_ass_subtitles(
    words: list[dict],
    *,
    out_path: pathlib.Path,
    video_w: int = 1080,
    video_h: int = 1920,
    chunk_words: int = 3,
) -> pathlib.Path:
    """Group words into 3-word chunks (Shorts-style) and write an .ass
    subtitle file ffmpeg can burn in.

    Style: large bold white sans-serif, neon green outline, centered,
    slightly above lower third. Matches the brand chrome on the stills.
    """
    chunks: list[dict] = []
    for i in range(0, len(words), chunk_words):
        group = words[i:i + chunk_words]
        if not group:
            continue
        chunks.append({
            "text": " ".join(w["word"] for w in group).strip(),
            "start": group[0]["start"],
            "end": group[-1]["end"],
        })

    header = (
        "[Script Info]\n"
        "Title: Glitch Short captions\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_w}\n"
        f"PlayResY: {video_h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # White text, neon green outline (BGR=88FF00), bold, large.
        # Alignment 2 = bottom-center; MarginV pushes it up from the bottom.
        "Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H0088FF00,"
        "&H00000000,1,0,0,0,100,100,0,0,1,4,2,2,80,80,420,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    def _ass_time(t: float) -> str:
        # H:MM:SS.cs (centiseconds)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t - 3600 * h - 60 * m
        return f"{h}:{m:02d}:{s:05.2f}"

    events = []
    for c in chunks:
        text = c["text"].replace("\n", " ").replace("{", "(").replace("}", ")")
        events.append(
            f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},"
            f"Default,,0,0,0,,{text}"
        )

    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    log.info("shorts.captions.ass_written", path=str(out_path), chunks=len(chunks))
    return out_path


def build_srt_fallback(words: list[dict], out_path: pathlib.Path) -> pathlib.Path:
    """Plain SRT — used as a fallback / debug surface. Not the primary
    burn-in source; .ass is preferred because it carries style info."""
    lines: list[str] = []
    for i, c in enumerate(_chunk(words, 3), start=1):
        lines.append(str(i))
        lines.append(f"{_srt_time(c['start'])} --> {_srt_time(c['end'])}")
        lines.append(c["text"])
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _chunk(words: list[dict], n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(words), n):
        g = words[i:i + n]
        if g:
            out.append({
                "text": " ".join(w["word"] for w in g),
                "start": g[0]["start"],
                "end": g[-1]["end"],
            })
    return out


def _srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - 3600 * h - 60 * m
    return f"{h:02d}:{m:02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"


def words_json_dump(words: list[dict], out_path: pathlib.Path) -> None:
    """Persist the raw word list for debugging / re-runs."""
    out_path.write_text(json.dumps(words, indent=2), encoding="utf-8")
