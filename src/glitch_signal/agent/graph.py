"""LangGraph StateGraph for the Glitch Social Media Agent video pipeline.

Two entry paths, chosen per-invocation via `state["content_source"]`:

  ai_generated (default, this project):
    scout → script_writer → storyboard → video_router → video_generator → END
    [scheduler re-enters at video_assembler when all shots complete]
    video_assembler → quality_check → [pass]     → END (Discord preview)
                                    → [retry]    → storyboard (retry_count < 2)
                                    → [escalate] → END (Discord alert)

  drive_footage (drive-footage — pre-edited clips from a Drive folder):
    drive_scout → caption_writer → END (Discord preview)
    [video generation + QC are bypassed — footage is post-ready]

Approval / preview / escalation surface lives in Discord now (host-bot
plugin polls DB for pending rows). The Telegram bot was retired
2026-05-01 — see commits removing the telegram/ module.
"""
from __future__ import annotations

import structlog
from langgraph.graph import END, StateGraph

from glitch_signal.agent.nodes.caption_writer import caption_writer_node
from glitch_signal.agent.nodes.drive_scout import drive_scout_node
from glitch_signal.agent.nodes.quality_check import quality_check_node
from glitch_signal.agent.nodes.scout import scout_node
from glitch_signal.agent.nodes.script_writer import script_writer_node
from glitch_signal.agent.nodes.storyboard import storyboard_node
from glitch_signal.agent.nodes.text_writer import text_writer_node
from glitch_signal.agent.nodes.video_assembler import video_assembler_node
from glitch_signal.agent.nodes.video_generator import video_generator_node
from glitch_signal.agent.nodes.video_router import video_router_node
from glitch_signal.agent.state import SignalAgentState

log = structlog.get_logger(__name__)

MAX_QC_RETRIES = 2


def _qc_router(state: SignalAgentState) -> str:
    if state.get("qc_passed"):
        return "pass"
    retry_count = int(state.get("retry_count") or 0)
    if retry_count < MAX_QC_RETRIES:
        return "retry"
    return "escalate"


async def _escalate_node(state: SignalAgentState) -> SignalAgentState:
    """QC escalation. Logs the failure; the Discord host-bot plugin will
    surface the row when its polling loop sees status='failed'."""
    asset_id = state.get("asset_id", "unknown")
    script_id = state.get("script_id", "unknown")
    qc_notes = state.get("qc_notes", "")

    log.error(
        "graph.qc_escalated",
        script_id=script_id, asset_id=asset_id,
        qc_notes=qc_notes[:200],
        retries=MAX_QC_RETRIES,
    )

    return {**state, "error": f"QC failed after {MAX_QC_RETRIES} retries: {qc_notes}"}


def _entry_router(state: SignalAgentState) -> str:
    """Pick the entry node based on content_source."""
    cs = (state.get("content_source") or "").strip().lower()
    return "drive_scout" if cs == "drive_footage" else "scout"


def _post_scout_router(state: SignalAgentState) -> str:
    """After scout discovers signals, pick the next node based on the brand's
    content_format. Text brands skip the video chain entirely.

    Also short-circuits to END if scout didn't find a signal worth processing
    (no signal_id in state means nothing to script).
    """
    from glitch_signal.config import brand_config

    if not state.get("signal_id"):
        return "end"

    brand_id = state.get("brand_id")
    if brand_id:
        try:
            fmt = (brand_config(brand_id).get("content_format") or "video").strip().lower()
            return "text_writer" if fmt == "text" else "script_writer"
        except KeyError:
            pass
    return "script_writer"


def build_graph() -> StateGraph:
    """Build and compile the full pipeline graph."""
    graph = StateGraph(SignalAgentState)

    # ai_generated branch (existing this project pipeline)
    graph.add_node("scout", scout_node)
    graph.add_node("text_writer", text_writer_node)
    graph.add_node("script_writer", script_writer_node)
    graph.add_node("storyboard", storyboard_node)
    graph.add_node("video_router", video_router_node)
    graph.add_node("video_generator", video_generator_node)
    graph.add_node("video_assembler", video_assembler_node)
    graph.add_node("quality_check", quality_check_node)
    graph.add_node("escalate", _escalate_node)

    # drive_footage branch (drive-footage)
    graph.add_node("drive_scout", drive_scout_node)
    graph.add_node("caption_writer", caption_writer_node)

    # Conditional entry — drive_footage brands skip the whole AI chain.
    graph.set_conditional_entry_point(
        _entry_router,
        {"scout": "scout", "drive_scout": "drive_scout"},
    )

    # ai_generated path — scout forks into text vs video based on brand config
    graph.add_conditional_edges(
        "scout",
        _post_scout_router,
        {
            "text_writer": "text_writer",
            "script_writer": "script_writer",
            "end": END,
        },
    )
    graph.add_edge("text_writer", END)   # text_writer marks status; Discord plugin polls
    graph.add_edge("script_writer", "storyboard")
    graph.add_edge("storyboard", "video_router")
    graph.add_edge("video_router", "video_generator")
    graph.add_edge("video_generator", END)

    # drive_footage path — no video gen, no assembler, no QC
    graph.add_edge("drive_scout", "caption_writer")
    graph.add_edge("caption_writer", END)

    # Assembler branch (scheduler-triggered re-entry for ai_generated)
    graph.add_edge("video_assembler", "quality_check")
    graph.add_conditional_edges(
        "quality_check",
        _qc_router,
        {
            "pass": END,
            "retry": "storyboard",
            "escalate": "escalate",
        },
    )
    graph.add_edge("escalate", END)

    return graph.compile()


# Singleton — built once per process
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
