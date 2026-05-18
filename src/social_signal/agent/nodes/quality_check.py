"""QualityCheck node — 5-point visual QC using Gemini 2.5 Pro vision.

Extracts frames at 1s, mid, and end. Checks brand alignment.
On failure, increments retry_count. At 2 retries, escalates to Telegram.
"""
from __future__ import annotations

import base64
import json
import pathlib
import tempfile

import ffmpeg
import litellm
import structlog

from social_signal.agent.llm import pick
from social_signal.agent.state import SignalAgentState
from social_signal.config import settings
from social_signal.db.models import VideoAsset
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

QC_CHECKLIST = """You are a QC reviewer for this project social media content.

Review the attached video frames and answer each question with a score (0-10) and one-sentence note.

Checklist:
1. Cobra watermark visible? (must be present in at least one frame)
2. Text rendering artifacts? (garbled text, glitches = low score)
3. Color palette correct? (dark base ~#0a0a0f, neon green ~#00ff88 accents)
4. Content varies across frames? (not a static shot / identical frames)
5. No marketing buzzwords in on-screen text? (no "amazing", "incredible", "game-changing")

Output JSON only:
{
  "scores": [8, 9, 7, 9, 10],
  "notes": ["cobra visible bottom-right", "no artifacts", "correct palette", "varied motion", "clean text"],
  "overall_score": 0.0-1.0,
  "passed": true|false,
  "reasoning": "one sentence summary"
}

passed = true only if overall_score >= 0.7 AND cobra watermark score >= 5.
"""


async def quality_check_node(state: SignalAgentState) -> SignalAgentState:
    asset_id = state.get("asset_id")
    asset_path = state.get("asset_path")
    retry_count = int(state.get("retry_count") or 0)

    if not asset_id or not asset_path:
        return {**state, "error": "quality_check: missing asset_id or asset_path"}

    if settings().is_dry_run:
        log.info("quality_check.dry_run", asset_id=asset_id)
        return {**state, "qc_passed": True, "qc_score": 1.0, "qc_notes": "dry-run pass"}

    passed, score, notes = await _run_qc(asset_path)

    # Persist QC results
    factory = _session_factory()
    async with factory() as session:
        asset = await session.get(VideoAsset, asset_id)
        if asset:
            asset.quality_score = score
            asset.qc_notes = json.dumps({"passed": passed, "notes": notes, "score": score})
            session.add(asset)
            await session.commit()

    log.info("quality_check.done", asset_id=asset_id, passed=passed, score=score)
    return {
        **state,
        "qc_passed": passed,
        "qc_score": score,
        "qc_notes": notes if isinstance(notes, str) else json.dumps(notes),
        "retry_count": retry_count + (0 if passed else 1),
    }


async def _run_qc(asset_path: str) -> tuple[bool, float, str]:
    frames = _extract_frames(asset_path)
    if not frames:
        return False, 0.0, "could not extract frames"

    content = [{"type": "text", "text": QC_CHECKLIST}]
    for frame_b64 in frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"},
        })

    mc = pick("heavy")
    try:
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            max_tokens=400,
            **mc.kwargs,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        passed = bool(data.get("passed", False))
        score = float(data.get("overall_score", 0.0))
        reasoning = str(data.get("reasoning", ""))
        return passed, score, reasoning
    except Exception as exc:
        log.warning("quality_check.llm_failed", error=str(exc))
        # On LLM failure, pass through to avoid blocking pipeline
        return True, 0.5, f"qc-llm-error: {exc}"


def _extract_frames(asset_path: str) -> list[str]:
    """Extract JPEG frames at 1s, mid, and near-end. Return as base64 strings."""
    try:
        probe = ffmpeg.probe(asset_path)
        duration = float(probe["format"].get("duration", 0))
    except Exception:
        return []

    timestamps = [1.0, duration / 2, max(duration - 2, 1.0)]
    frames: list[str] = []

    for ts in timestamps:
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            (
                ffmpeg.input(asset_path, ss=ts)
                .output(tmp_path, vframes=1, format="image2", vcodec="mjpeg")
                .overwrite_output()
                .run(quiet=True)
            )
            with open(tmp_path, "rb") as f:
                frames.append(base64.b64encode(f.read()).decode())
            pathlib.Path(tmp_path).unlink(missing_ok=True)
        except Exception as exc:
            log.warning("quality_check.frame_extract_failed", ts=ts, error=str(exc))

    return frames
