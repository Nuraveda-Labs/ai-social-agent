"""VideoRouter node — deterministic model selection, no LLM.

Reads the routing table from brand.config.json so routing can be updated
without a code deploy. Phase 1: all hints map to kling_2.
"""
from __future__ import annotations

import structlog

from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import brand_config, settings

log = structlog.get_logger(__name__)

# Default settings per model
_MODEL_SETTINGS: dict[str, dict] = {
    "kling_2":     {"cfg_scale": 0.5, "quality": "standard"},
    "runway_gen4": {"ratio": "9:16",  "watermark": False},
    "veo_3":       {"aspect_ratio": "9:16"},
    "hailuo":      {"quality": "standard"},
    "mock":        {},
}


async def video_router_node(state: SignalAgentState) -> SignalAgentState:
    shots: list[dict] = state.get("shots", [])
    if not shots:
        return {**state, "error": "video_router: no shots in state", "routed_shots": []}

    brand_id = state.get("brand_id") or settings().default_brand_id
    routing_table: dict[str, str] = (
        brand_config(brand_id)
        .get("video_model_routing", {})
        .get("model_map", {})
    )

    # If dry_run, override everything to "mock"
    force_mock = settings().is_dry_run

    routed: list[dict] = []
    for shot in shots:
        hint = shot.get("model_hint", "cinematic")
        model = "mock" if force_mock else routing_table.get(hint, "kling_2")
        routed.append({
            **shot,
            "model": model,
            "model_settings": _MODEL_SETTINGS.get(model, {}),
        })

    log.info(
        "video_router.done",
        n_shots=len(routed),
        models=list({s["model"] for s in routed}),
    )
    return {**state, "routed_shots": routed}
