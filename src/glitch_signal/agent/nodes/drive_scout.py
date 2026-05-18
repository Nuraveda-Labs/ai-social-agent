"""DriveScout node — polls a brand's Drive folder for new pre-edited clips.

Entry point for the `content_source: drive_footage` pipeline. For each
unseen Drive file it:
  1. Downloads to {video_storage_path}/drive/<brand_id>/<file_id>.mp4
  2. Creates a Signal row with source="drive", source_ref=file_id
  3. Emits the first new signal_id into state so the rest of the graph
     (caption_writer → telegram_preview) processes it.

Dedup: a file is considered seen when a Signal with
(brand_id, source="drive", source_ref=file_id) already exists.
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime

import structlog
from sqlmodel import select

from glitch_signal.agent.state import SignalAgentState
from glitch_signal.config import brand_config, resolve_publish_platform, settings
from glitch_signal.db.models import Signal
from glitch_signal.db.session import _session_factory
from glitch_signal.integrations import google_drive

log = structlog.get_logger(__name__)


async def drive_scout_node(state: SignalAgentState) -> SignalAgentState:
    brand_id = state.get("brand_id") or settings().default_brand_id
    cfg = brand_config(brand_id)

    if cfg.get("content_source") != "drive_footage":
        return {
            **state,
            "error": (
                f"drive_scout: brand {brand_id!r} content_source is "
                f"{cfg.get('content_source')!r}, expected 'drive_footage'"
            ),
        }

    folder_id = (
        cfg.get("drive_folder_id")
        or cfg.get("platforms", {}).get("drive_folder_id")
    )
    if not folder_id:
        return {
            **state,
            "error": f"drive_scout: no drive_folder_id in brand config for {brand_id!r}",
        }

    try:
        files = await google_drive.list_video_files(folder_id)
    except Exception as exc:
        log.error("drive_scout.list_failed", brand=brand_id, error=str(exc))
        return {**state, "error": f"drive_scout: list failed: {exc}"}

    if not files:
        log.info("drive_scout.empty_folder", brand=brand_id, folder_id=folder_id)
        return {**state, "brand_id": brand_id, "signals": []}

    new_signals: list[dict] = []
    # We only need the storage root to COMPUTE the expected local path
    # that publisher.py will download to later. We don't create the file
    # or the folder eagerly — that's publisher's job at post time.
    storage_root = pathlib.Path(settings().video_storage_path) / "drive" / brand_id

    factory = _session_factory()
    async with factory() as session:
        for f in files:
            if await _already_seen(session, brand_id, f.id):
                continue

            # Deterministic local path where publisher.py will download the
            # Drive file just before posting. Extension follows the Drive
            # filename (fallback .mp4 for extension-less files).
            suffix = pathlib.Path(f.name).suffix.lower() or ".mp4"
            local_path = storage_root / f"{f.id}{suffix}"

            sig = Signal(
                id=str(uuid.uuid4()),
                brand_id=brand_id,
                source="drive",
                source_ref=f.id,
                summary=f"Drive clip: {f.name}",
                novelty_score=1.0,   # drive clips are always "keep" — no scoring phase
                status="queued",
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(sig)
            new_signals.append({
                "id": sig.id,
                "source_ref": f.id,
                "name": f.name,
                "local_path": str(local_path),
                "size": f.size,
            })

        await session.commit()

    log.info(
        "drive_scout.listed",
        brand=brand_id,
        new_signals=len(new_signals),
        jit_download=True,
    )

    # Mirror newly-queued files into the brand's Google Sheet output (if
    # one is configured on the video_uploader task). Status starts as
    # `queued` — caption_writer + publisher flip it forward as work
    # progresses. Done outside the DB transaction so a Sheets outage
    # never blocks Signal creation.
    await _mirror_to_sheet(brand_id, new_signals)

    log.info(
        "drive_scout.done",
        brand=brand_id,
        new=len(new_signals),
        total_in_folder=len(files),
    )

    # If the graph was invoked for a specific signal_id (manual re-run),
    # keep it. Otherwise promote the first new signal so the downstream
    # nodes have something to process.
    first_new_id = new_signals[0]["id"] if new_signals else ""

    # Resolve the publisher to use for this brand. Priority order (defined
    # in glitch_signal.config._PUBLISH_PRIORITY):
    #   upload_post_tiktok → zernio_tiktok → direct tiktok
    # First one with `enabled=true` on the brand's platforms block wins.
    # Explicit state["platform"] still overrides (manual test harness,
    # specific re-runs).
    try:
        default_platform = resolve_publish_platform(brand_id, "tiktok")
    except RuntimeError as exc:
        log.warning(
            "drive_scout.no_publisher_configured",
            brand=brand_id,
            error=str(exc)[:200],
        )
        default_platform = "tiktok"

    return {
        **state,
        "brand_id": brand_id,
        "signal_id": state.get("signal_id") or first_new_id,
        "signals": new_signals,
        "platform": state.get("platform") or default_platform,
    }


async def _already_seen(session, brand_id: str, drive_file_id: str) -> bool:
    result = await session.execute(
        select(Signal).where(
            Signal.brand_id == brand_id,
            Signal.source == "drive",
            Signal.source_ref == drive_file_id,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Google Sheet mirror — one row per newly-discovered Drive file.
#
# The brand config opts in via
#   tasks.video_uploader.outputs.google_sheet.{sheet_id, worksheet}
# Missing block / any API error is logged but never fails the scout run
# — the agent's internal DB is still the source of truth.
# ---------------------------------------------------------------------------

async def _mirror_to_sheet(brand_id: str, new_signals: list[dict]) -> None:
    from glitch_signal.integrations import sheet_tracker
    from glitch_signal.media.filename_parser import parse as parse_filename

    if not new_signals:
        return
    if not await sheet_tracker.ensure_header(brand_id):
        return

    for sig in new_signals:
        parsed = parse_filename(sig["name"])
        await sheet_tracker.append_new_video(
            brand_id,
            video_name=sig["name"],
            drive_file_id=sig["source_ref"],
            product=parsed.product,
            variant_group=parsed.variant_group,
            geo=parsed.geo,
        )
