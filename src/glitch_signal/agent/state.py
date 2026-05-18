"""LangGraph state schema for the Glitch Social Media Agent video pipeline."""
from __future__ import annotations

from typing_extensions import TypedDict


class SignalAgentState(TypedDict, total=False):
    # --- Brand (threaded through every node) ---
    brand_id: str               # key into settings().brands; defaults to default_brand_id
    content_source: str         # ai_generated | drive_footage — drives graph entry

    # --- Input / Scout ---
    signal_id: str
    platform: str               # youtube_shorts | twitter | instagram_reels | tiktok
    signals: list[dict]         # list of discovered Signal dicts from Scout

    # --- ScriptWriter ---
    script_id: str
    script_body: str
    content_type: str           # cinematic | product | technical | data
    key_visuals: list[str]

    # --- Storyboard ---
    shots: list[dict]           # [{visual, duration_s, model_hint}]

    # --- VideoRouter (deterministic) ---
    routed_shots: list[dict]    # shots + {model, settings}

    # --- VideoGenerator ---
    video_job_ids: list[str]
    all_shots_done: bool        # False after dispatch; scheduler sets True

    # --- VideoAssembler (scheduler-triggered re-entry) ---
    asset_id: str
    asset_path: str

    # --- QualityCheck ---
    qc_passed: bool
    qc_score: float
    qc_notes: str

    # --- TelegramPreview ---
    preview_sent: bool
    veto_deadline: str          # ISO datetime string

    # --- Error handling ---
    error: str | None
    retry_count: int
