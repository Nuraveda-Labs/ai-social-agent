"""Scheduler — Python port of glitch-cod-confirm/src/lib/scheduler.js.

Six concurrent tick functions run every SCHEDULER_TICK_MS (default 30s):

1. dispatch_video_jobs       — poll VideoJob(dispatched), call model.poll(), update status
2. check_shots_complete      — for ContentScript(generating), if all shots done → trigger assembler
3. promote_veto_windows      — ScheduledPost(pending_veto, deadline ≤ now) → queued
4. dispatch_scheduled_posts  — ScheduledPost(queued, scheduled_for ≤ now) → dispatching → publish
5. send_orm_auto_responses   — OrmResponse(pending_review, auto_send_at ≤ now) → send
6. sweep_stuck               — ScheduledPost(dispatching, last_attempt > 5min) → requeue

DISPATCH_MODE=dry_run: all ticks log intent but make no external calls.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlmodel import select

from social_signal.config import settings
from social_signal.db.models import (
    ContentScript,
    OrmResponse,
    ScheduledPost,
    VideoJob,
)
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

_running = False
_stop_event: asyncio.Event | None = None


def start() -> None:
    """Start the scheduler loop as a background asyncio task."""
    global _running, _stop_event
    if _running:
        return
    _running = True
    _stop_event = asyncio.Event()
    asyncio.create_task(_loop())
    log.info("scheduler.started", tick_ms=settings().scheduler_tick_ms)


def stop() -> None:
    global _running
    _running = False
    if _stop_event:
        _stop_event.set()
    log.info("scheduler.stopped")


async def _loop() -> None:
    tick_s = settings().scheduler_tick_ms / 1000
    while _running:
        try:
            await _tick()
        except Exception as exc:
            log.error("scheduler.tick_error", error=str(exc))
        await asyncio.sleep(tick_s)


async def _tick() -> None:
    await asyncio.gather(
        _dispatch_video_jobs(),
        _check_shots_complete(),
        _promote_veto_windows(),
        _dispatch_scheduled_posts(),
        _send_orm_auto_responses(),
        _sweep_stuck(),
        _poll_orm_mentions(),
        _reconcile_awaiting_webhook(),
        _pull_post_analytics(),
        _cleanup_posted_media(),
        _sweep_comments_tick(),
        _sheet_posting_tick(),
        _sheet_reconcile_tick(),
        return_exceptions=True,
    )


_sheet_reconcile_last: datetime | None = None


async def _sheet_reconcile_tick() -> None:
    """Every 10 minutes, backfill real platform_post_ids for rows whose
    post was accepted asynchronously and only got a `request:xxx` placeholder
    on first write."""
    global _sheet_reconcile_last

    if not settings().glitch_posts_sheet_id:
        return

    now = datetime.now(UTC).replace(tzinfo=None)
    if _sheet_reconcile_last and (now - _sheet_reconcile_last) < timedelta(minutes=10):
        return
    _sheet_reconcile_last = now

    try:
        from social_signal.sheet_posting.reconciler import reconcile_pending

        summary = await reconcile_pending()
        if summary.get("reconciled"):
            log.info("scheduler.sheet_reconcile_done", **summary)
    except Exception as exc:
        log.warning("scheduler.sheet_reconcile_failed", error=str(exc)[:200])


_sheet_posting_last: datetime | None = None


async def _sheet_posting_tick() -> None:
    """Fire one sheet-driven post if one is due. Rate-limited to every 5 min.

    The pacing rules inside reader.fetch_next_due (min interval + daily cap
    per brand/platform) mean that firing every 5 min is safe — most ticks
    will return None and no post goes out. When a post IS due, one row is
    posted and marked in the sheet.
    """
    global _sheet_posting_last

    if not settings().glitch_posts_sheet_id:
        return

    now = datetime.now(UTC).replace(tzinfo=None)
    if _sheet_posting_last and (now - _sheet_posting_last) < timedelta(minutes=5):
        return
    _sheet_posting_last = now

    try:
        from social_signal.sheet_posting.poster import post_one
        from social_signal.sheet_posting.reader import fetch_next_due

        row = await fetch_next_due(now=now)
        if not row:
            return
        await post_one(row)
    except Exception as exc:
        log.warning("scheduler.sheet_posting_tick_failed", error=str(exc)[:200])


_comment_sweep_last: datetime | None = None


async def _sweep_comments_tick() -> None:
    """Fire comments.sweep_comments() at most once every 10 minutes.

    The scheduler tick itself runs every 30s; we don't need that cadence for
    comment polling. 10 min keeps the engagement window tight without
    spamming Upload-Post's get_post_comments endpoint.
    """
    global _comment_sweep_last

    now = datetime.now(UTC).replace(tzinfo=None)
    if _comment_sweep_last and (now - _comment_sweep_last) < timedelta(minutes=10):
        return
    _comment_sweep_last = now

    from social_signal.comments.sweeper import sweep_comments

    try:
        await sweep_comments()
    except Exception as exc:
        log.warning("scheduler.comments_sweep_failed", error=str(exc)[:200])


async def _cleanup_posted_media() -> None:
    """Delete local video files whose post went live > N minutes ago.

    Scope:
      - Local VideoAsset.file_path (the original download)
      - ffmpeg transform siblings in the same directory
        (e.g. <stem>.strip_audio<ext>, <stem>.<any_transform><ext>)

    Does NOT touch:
      - Drive source files (client-owned)
      - DB rows (audit trail)
      - Google Sheet rows (historical visibility)

    Idempotent — running against an already-cleaned PublishedPost is a
    no-op. Never raises; a filesystem error on one post doesn't block
    the rest of the batch.
    """
    import pathlib

    from social_signal.db.models import PublishedPost, VideoAsset

    s = settings()
    after_s = s.media_cleanup_after_minutes * 60
    if after_s <= 0:
        return
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=after_s)

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PublishedPost)
            .where(PublishedPost.published_at <= cutoff)
            .order_by(PublishedPost.published_at.desc())
            .limit(s.media_cleanup_batch)
        )
        posts = result.scalars().all()

    freed_bytes = 0
    deleted = 0
    for pub in posts:
        try:
            factory = _session_factory()
            async with factory() as session:
                sp = await session.get(ScheduledPost, pub.scheduled_post_id)
                if not sp:
                    continue
                asset = await session.get(VideoAsset, sp.asset_id)
                if not asset or not asset.file_path:
                    continue

            original = pathlib.Path(asset.file_path)
            targets: list[pathlib.Path] = []
            if original.exists():
                targets.append(original)

            # Transform siblings live in the same directory with a
            # deterministic suffix. ffmpeg.apply_transforms outputs them
            # as <stem>.<transform_name>.<ext>. Match the known set to
            # avoid deleting anything unrelated that happens to share a
            # stem.
            parent = original.parent
            if parent.exists():
                known_transforms = {"strip_audio"}
                for p in parent.glob(f"{original.stem}.*{original.suffix}"):
                    mid = p.stem[len(original.stem) + 1:]
                    if mid in known_transforms and p.exists():
                        targets.append(p)

            for t in targets:
                try:
                    size = t.stat().st_size
                    t.unlink()
                    freed_bytes += size
                    deleted += 1
                    log.info(
                        "scheduler.media_cleaned",
                        published_post_id=pub.id,
                        path=str(t),
                        bytes=size,
                    )
                except FileNotFoundError:
                    pass   # raced with another cleanup / manual delete
                except OSError as exc:
                    log.warning(
                        "scheduler.media_cleanup_error",
                        published_post_id=pub.id,
                        path=str(t),
                        error=str(exc)[:200],
                    )
        except Exception as exc:
            log.warning(
                "scheduler.media_cleanup_post_error",
                published_post_id=pub.id,
                error=str(exc)[:200],
            )

    if deleted:
        log.info(
            "scheduler.media_cleanup_batch",
            files_deleted=deleted,
            freed_mb=round(freed_bytes / 1024 / 1024, 1),
        )


async def _pull_post_analytics() -> None:
    """Fetch per-post analytics from Upload-Post for due PublishedPost rows."""
    try:
        from social_signal.analytics.upload_post import sweep_due_posts
        updated = await sweep_due_posts(limit=settings().analytics_sweep_batch)
        if updated:
            log.info("scheduler.analytics_swept", count=len(updated))
    except Exception as exc:
        log.warning("scheduler.analytics_sweep_error", error=str(exc)[:200])


# ---------------------------------------------------------------------------
# 1. dispatch_video_jobs — poll dispatched VideoJob rows
# ---------------------------------------------------------------------------

async def _dispatch_video_jobs() -> None:
    from social_signal.video_models.kling import get_model

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(VideoJob).where(VideoJob.status == "dispatched")
        )
        jobs = result.scalars().all()

    for job in jobs:
        if not job.api_job_id:
            continue
        try:
            model = get_model(job.model if job.model != "mock" else "kling_2")
            result_obj = await model.poll(job.api_job_id)

            factory = _session_factory()
            async with factory() as session:
                j = await session.get(VideoJob, job.id)
                if not j:
                    continue
                if result_obj.status == "done":
                    j.status = "done"
                    j.video_url = result_obj.video_url
                    j.completed_at = datetime.now(UTC).replace(tzinfo=None)
                    if result_obj.cost_usd:
                        j.cost_usd = result_obj.cost_usd
                elif result_obj.status == "failed":
                    j.status = "failed"
                    j.last_error = result_obj.error or "model reported failure"
                    j.completed_at = datetime.now(UTC).replace(tzinfo=None)
                else:
                    j.status = "dispatched"  # still processing
                session.add(j)
                await session.commit()

            if result_obj.status in ("done", "failed"):
                log.info(
                    "scheduler.video_job_updated",
                    job_id=job.id,
                    status=result_obj.status,
                )
        except Exception as exc:
            log.warning("scheduler.video_job_poll_error", job_id=job.id, error=str(exc))


# ---------------------------------------------------------------------------
# 2. check_shots_complete — trigger assembler when all shots done
# ---------------------------------------------------------------------------

async def _check_shots_complete() -> None:
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ContentScript).where(ContentScript.status == "generating")
        )
        scripts = result.scalars().all()

    for cs in scripts:
        factory = _session_factory()
        async with factory() as session:
            result = await session.execute(
                select(VideoJob).where(VideoJob.script_id == cs.id)
            )
            jobs = result.scalars().all()

        if not jobs:
            continue

        failed = [j for j in jobs if j.status == "failed"]
        done = [j for j in jobs if j.status == "done"]

        if failed:
            log.warning(
                "scheduler.shots_failed",
                script_id=cs.id,
                n_failed=len(failed),
            )
            continue

        if len(done) == len(jobs):
            log.info("scheduler.all_shots_done", script_id=cs.id, n_shots=len(done))
            await _trigger_assembler(cs.id)


async def _trigger_assembler(script_id: str) -> None:
    """Re-invoke the LangGraph pipeline from the video_assembler node."""
    from social_signal.agent.graph import build_graph

    graph = build_graph()
    state = {"script_id": script_id, "all_shots_done": True}
    try:
        await graph.ainvoke(state, config={"entry_override": "video_assembler"})
    except Exception as exc:
        log.error("scheduler.assembler_trigger_failed", script_id=script_id, error=str(exc))


# ---------------------------------------------------------------------------
# 3. promote_veto_windows — pending_veto → queued when deadline passes
# ---------------------------------------------------------------------------

async def _promote_veto_windows() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.status == "pending_veto",
                ScheduledPost.veto_deadline <= now,
            )
        )
        posts = result.scalars().all()

        for sp in posts:
            sp.status = "queued"
            session.add(sp)
            log.info("scheduler.veto_window_expired", scheduled_post_id=sp.id)
        await session.commit()


# ---------------------------------------------------------------------------
# 4. dispatch_scheduled_posts — queued → dispatching → publish
#
# Brands WITH a `tasks.video_uploader.posting_rules` block get
# variant-aware dispatch: at most one post per tick per brand, gated by
# slots / daily_cap / min_interval / skip_patterns, and chosen so two
# near-duplicate Meta ad variants aren't posted back-to-back on the
# TikTok grid.
#
# Brands WITHOUT rules keep the legacy "claim up to 10" behaviour so the
# ai_generated this project pipeline is unchanged.
# ---------------------------------------------------------------------------

async def _dispatch_scheduled_posts() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.status == "queued",
                ScheduledPost.scheduled_for <= now,
            )
        )
        all_candidates = result.scalars().all()

    if not all_candidates:
        return

    # Partition candidates: rules-governed brands vs legacy brands.
    from collections import defaultdict
    by_brand: dict[str, list[ScheduledPost]] = defaultdict(list)
    for sp in all_candidates:
        by_brand[sp.brand_id].append(sp)

    claimed: list[ScheduledPost] = []

    for brand_id, brand_candidates in by_brand.items():
        rules = _posting_rules_for(brand_id)
        if rules is None:
            # Legacy path — claim up to 10, no gating.
            for sp in brand_candidates[:10]:
                claimed.append(sp)
            continue

        picked = await _pick_with_rules(brand_id, brand_candidates, rules, now)
        if picked is not None:
            claimed.append(picked)
        else:
            log.info(
                "scheduler.dispatch_skipped_by_rules",
                brand_id=brand_id,
                candidates=len(brand_candidates),
            )

    if not claimed:
        return

    # Atomically flip status for claimed rows.
    factory = _session_factory()
    async with factory() as session:
        ids = [sp.id for sp in claimed]
        result = await session.execute(
            select(ScheduledPost).where(ScheduledPost.id.in_(ids))
        )
        rows = result.scalars().all()
        for sp in rows:
            sp.status = "dispatching"
            sp.last_attempt_at = now
            session.add(sp)
        await session.commit()

    for sp in claimed:
        if settings().is_dry_run:
            log.info("scheduler.dry_run_publish", scheduled_post_id=sp.id, platform=sp.platform)
            factory = _session_factory()
            async with factory() as session:
                s = await session.get(ScheduledPost, sp.id)
                if s:
                    s.status = "done"
                    await session.commit()
            continue

        try:
            from social_signal.agent.nodes.publisher import publish
            await publish(sp.id)
        except Exception as exc:
            log.error("scheduler.publish_error", scheduled_post_id=sp.id, error=str(exc))


# ---------------------------------------------------------------------------
# Variant-aware picker — called for brands that declare posting_rules.
# ---------------------------------------------------------------------------

def _posting_rules_for(brand_id: str) -> dict | None:
    """Return the brand's video_uploader posting_rules block, or None."""
    try:
        from social_signal.config import brand_config
        cfg = brand_config(brand_id)
    except Exception:
        return None
    task = (cfg.get("tasks") or {}).get("video_uploader") or {}
    if not task.get("enabled", False):
        return None
    rules = task.get("posting_rules")
    if not rules:
        return None
    return rules


async def _pick_with_rules(
    brand_id: str,
    candidates: list[ScheduledPost],
    rules: dict,
    now: datetime,
) -> ScheduledPost | None:
    """Pick at most one ScheduledPost that satisfies every rule."""
    from zoneinfo import ZoneInfo

    # Gate 1 — slots. Convert `now` to the brand timezone before comparing.
    slots: list[str] = rules.get("slots_local") or []
    if slots:
        from social_signal.config import brand_config
        tz = brand_config(brand_id).get("timezone", "UTC")
        try:
            local_now = now.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))
        except Exception:
            local_now = now.replace(tzinfo=UTC)
        if not _in_any_slot(local_now, slots, tolerance_minutes=15):
            return None

    # Gate 2 — daily cap (uses PublishedPost as truth).
    cap = rules.get("daily_cap")
    if cap is not None:
        posted_today = await _count_posts_today(brand_id, now)
        if posted_today >= cap:
            return None

    # Gate 3 — min interval since last post.
    min_int = rules.get("min_interval_minutes") or 0
    if min_int > 0:
        mins = await _minutes_since_last_post(brand_id, now)
        if mins is not None and mins < min_int:
            return None

    # Gate 4 — skip patterns (match on any recent Drive filename the row
    # carries via product/variant_group; if the filename match were needed
    # we'd have to join Signal, so we check the asset file path instead).
    skip_patterns: list[str] = [p.lower() for p in rules.get("skip_patterns") or []]

    # Load recent post history (variant_groups + products).
    lookback = max(rules.get("variant_gap") or 0, rules.get("product_gap") or 0)
    recent_variant_groups, recent_products = await _recent_brand_post_keys(
        brand_id, limit=lookback
    )

    # Order candidates.
    order = rules.get("order", "oldest_first")
    if order == "newest_first":
        candidates = sorted(candidates, key=lambda sp: sp.scheduled_for, reverse=True)
    else:
        candidates = sorted(candidates, key=lambda sp: sp.scheduled_for)

    # First pass — strict (both gaps enforced).
    pick = _first_eligible(
        candidates,
        recent_variant_groups=recent_variant_groups,
        recent_products=recent_products,
        variant_gap=rules.get("variant_gap") or 0,
        product_gap=rules.get("product_gap") or 0,
        skip_patterns=skip_patterns,
    )
    if pick:
        return pick

    # Starvation guard: relax product_gap first.
    pick = _first_eligible(
        candidates,
        recent_variant_groups=recent_variant_groups,
        recent_products=[],
        variant_gap=rules.get("variant_gap") or 0,
        product_gap=0,
        skip_patterns=skip_patterns,
    )
    if pick:
        log.info(
            "scheduler.dispatch_relaxed_product_gap",
            brand_id=brand_id, scheduled_post_id=pick.id,
        )
        return pick

    # Still nothing — relax variant_gap too (truly starved queue).
    pick = _first_eligible(
        candidates,
        recent_variant_groups=[],
        recent_products=[],
        variant_gap=0,
        product_gap=0,
        skip_patterns=skip_patterns,
    )
    if pick:
        log.info(
            "scheduler.dispatch_relaxed_variant_gap",
            brand_id=brand_id, scheduled_post_id=pick.id,
        )
    return pick


def _first_eligible(
    candidates: list[ScheduledPost],
    *,
    recent_variant_groups: list[str],
    recent_products: list[str],
    variant_gap: int,
    product_gap: int,
    skip_patterns: list[str],
) -> ScheduledPost | None:
    """Walk candidates in order, return the first that passes every rule."""
    recent_variants_window = recent_variant_groups[:variant_gap] if variant_gap else []
    recent_products_window = recent_products[:product_gap] if product_gap else []

    for sp in candidates:
        if skip_patterns and sp.variant_group:
            lvg = sp.variant_group.lower()
            if any(p in lvg for p in skip_patterns):
                continue
        if sp.variant_group and sp.variant_group in recent_variants_window:
            continue
        if sp.product and sp.product in recent_products_window:
            continue
        return sp
    return None


def _in_any_slot(local_now: datetime, slots: list[str], *, tolerance_minutes: int) -> bool:
    """True if `local_now` is within `tolerance_minutes` of any HH:MM slot."""
    now_mins = local_now.hour * 60 + local_now.minute
    for s in slots:
        try:
            hh, mm = [int(x) for x in s.split(":")]
        except ValueError:
            continue
        slot_mins = hh * 60 + mm
        if abs(now_mins - slot_mins) <= tolerance_minutes:
            return True
    return False


async def _count_posts_today(brand_id: str, now: datetime) -> int:
    """Count posts already fired today — both finalised AND in-flight.

    Upload-Post is webhook-async: a dispatch claims a ScheduledPost and
    marks it `awaiting_webhook`, but the matching PublishedPost row isn't
    written until the webhook (or the 10-min reconciliation fallback)
    finalises. A naive count of just `PublishedPost` makes the daily_cap
    gate blind during that window and the dispatcher re-fires every 30s
    tick until the slot closes — observed 2026-04-18 as 11 posts fired
    inside a single 15-min slot when cap was 3.

    We count any ScheduledPost that has entered the publish pipeline
    today (dispatching, awaiting_webhook, done) PLUS PublishedPost
    timestamps — covers both the async and sync paths without
    double-counting (we look at distinct scheduled_post ids implicitly).
    """
    from social_signal.db.models import ScheduledPost
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.brand_id == brand_id,
                ScheduledPost.status.in_(("dispatching", "awaiting_webhook", "done")),
                ScheduledPost.last_attempt_at >= day_start,
            )
        )
        return len(result.scalars().all())


async def _minutes_since_last_post(brand_id: str, now: datetime) -> float | None:
    """Return minutes since the most recent dispatch for this brand.

    Uses ScheduledPost.last_attempt_at rather than PublishedPost.published_at
    so the min_interval gate is honoured *during* the async webhook window,
    not just after it. See the docstring on `_count_posts_today` for the
    rationale (same failure mode observed on 2026-04-18).
    """
    from social_signal.db.models import ScheduledPost
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.brand_id == brand_id,
                ScheduledPost.status.in_(("dispatching", "awaiting_webhook", "done")),
                ScheduledPost.last_attempt_at.is_not(None),
            )
            .order_by(ScheduledPost.last_attempt_at.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
    if latest is None or latest.last_attempt_at is None:
        return None
    return (now - latest.last_attempt_at).total_seconds() / 60.0


async def _recent_brand_post_keys(
    brand_id: str, *, limit: int
) -> tuple[list[str], list[str]]:
    """Return (variant_groups, products) for the brand's last `limit` posts,
    newest first. Joins PublishedPost→ScheduledPost so we read the
    parsed-filename fields off the scheduled row."""
    if limit <= 0:
        return [], []
    from social_signal.db.models import PublishedPost
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PublishedPost)
            .where(PublishedPost.brand_id == brand_id)
            .order_by(PublishedPost.published_at.desc())
            .limit(limit)
        )
        pubs = result.scalars().all()
        variant_groups: list[str] = []
        products: list[str] = []
        for pub in pubs:
            sp = await session.get(ScheduledPost, pub.scheduled_post_id)
            if not sp:
                continue
            if sp.variant_group:
                variant_groups.append(sp.variant_group)
            if sp.product:
                products.append(sp.product)
    return variant_groups, products


# ---------------------------------------------------------------------------
# 5. send_orm_auto_responses
# ---------------------------------------------------------------------------

async def _send_orm_auto_responses() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(OrmResponse).where(
                OrmResponse.status == "pending_review",
                OrmResponse.auto_send_at <= now,
            )
        )
        responses = result.scalars().all()

    for orm_resp in responses:
        try:
            from social_signal.orm.responder import send_approved_response
            await send_approved_response(orm_resp.id)
        except Exception as exc:
            log.warning(
                "scheduler.orm_auto_send_error",
                orm_resp_id=orm_resp.id,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# 6. sweep_stuck — dispatching > 5min → requeue with backoff
# ---------------------------------------------------------------------------

async def _sweep_stuck() -> None:
    stuck_after_s = settings().scheduler_stuck_after_ms / 1000
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=stuck_after_s)

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.status == "dispatching",
                ScheduledPost.last_attempt_at <= cutoff,
            )
        )
        stuck = result.scalars().all()

        for sp in stuck:
            log.warning("scheduler.stuck_job", scheduled_post_id=sp.id, attempts=sp.attempts)
            now = datetime.now(UTC).replace(tzinfo=None)
            s = settings()

            if sp.attempts >= 3:
                sp.status = "failed"
                sp.last_error = "max retries exceeded"
            elif sp.attempts == 2:
                sp.status = "queued"
                sp.scheduled_for = now + timedelta(milliseconds=s.publish_retry_2_ms)
            else:
                sp.status = "queued"
                sp.scheduled_for = now + timedelta(milliseconds=s.publish_retry_1_ms)

            session.add(sp)
        await session.commit()


# ---------------------------------------------------------------------------
# 7. reconcile_awaiting_webhook — fallback for Upload-Post webhooks that
# don't arrive within the reconcile window. Polls get_status once and
# finalizes the ScheduledPost if the vendor has published.
# ---------------------------------------------------------------------------

async def _reconcile_awaiting_webhook() -> None:
    import uuid as _uuid

    from social_signal.db.models import PublishedPost
    from social_signal.platforms.upload_post import (
        _PLATFORM_MAP,
        poll_status_for_request,
    )

    s = settings()
    window_s = s.upload_post_webhook_reconcile_after_s
    if window_s <= 0:
        return

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=window_s)

    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ScheduledPost).where(
                ScheduledPost.status == "awaiting_webhook",
                ScheduledPost.last_attempt_at <= cutoff,
            ).limit(10)
        )
        candidates = result.scalars().all()

    for sp in candidates:
        if not sp.vendor_request_id:
            continue

        # Dispatch to the right vendor poll based on platform prefix.
        # Buffer and Upload-Post share the `awaiting_webhook` status and
        # the `vendor_request_id` column (Buffer stores a post id there,
        # Upload-Post a request id). The ScheduledPost.platform tells
        # us which vendor to ask.
        try:
            if sp.platform.startswith("buffer_"):
                from social_signal.config import brand_config
                from social_signal.platforms.buffer import poll_status_for_post
                cfg_block = (
                    brand_config(sp.brand_id).get("platforms", {}).get(sp.platform, {})
                    or {}
                )
                organization_id = cfg_block.get("organization_id")
                if not organization_id:
                    log.warning(
                        "scheduler.reconcile_missing_buffer_org",
                        scheduled_post_id=sp.id,
                        platform=sp.platform,
                    )
                    continue
                ppid, url = await poll_status_for_post(
                    sp.vendor_request_id, organization_id
                )
            else:
                target = _PLATFORM_MAP.get(sp.platform)
                if not target:
                    continue
                ppid, url = await poll_status_for_request(sp.vendor_request_id, target)
        except Exception as exc:
            log.warning(
                "scheduler.reconcile_awaiting_webhook_error",
                scheduled_post_id=sp.id,
                request_id=sp.vendor_request_id,
                platform=sp.platform,
                error=str(exc)[:200],
            )
            # Mark failed on hard error from the vendor; keep otherwise so
            # the next tick can retry.
            factory = _session_factory()
            async with factory() as session:
                s_row = await session.get(ScheduledPost, sp.id)
                if s_row:
                    s_row.status = "failed"
                    s_row.last_error = str(exc)[:1000]
                    session.add(s_row)
                    await session.commit()
            continue

        if not ppid and not url:
            # Still in flight — leave awaiting_webhook; try again next tick.
            continue

        factory = _session_factory()
        async with factory() as session:
            s_row = await session.get(ScheduledPost, sp.id)
            if not s_row:
                continue
            existing = (await session.execute(
                select(PublishedPost).where(PublishedPost.scheduled_post_id == sp.id)
            )).scalar_one_or_none()
            if existing:
                s_row.status = "done"
                session.add(s_row)
                await session.commit()
                continue
            pub = PublishedPost(
                id=str(_uuid.uuid4()),
                brand_id=s_row.brand_id,
                scheduled_post_id=s_row.id,
                platform=s_row.platform,
                platform_post_id=ppid or sp.vendor_request_id,
                platform_url=url,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
            s_row.status = "done"
            session.add(pub)
            session.add(s_row)
            await session.commit()
            # Brain mirror (GROW-BIND-3): notify sibling agents on the
            # same brand that a publish just succeeded. Silently no-ops
            # when BSK-004 brain token isn't set for this brand_id.
            # Fire-and-forget; brain failures are caught + logged and
            # never roll back the PublishedPost row above.
            from social_signal.brain import schedule_published_post_mirror
            schedule_published_post_mirror(
                brand_id=s_row.brand_id,
                platform=s_row.platform,
                platform_post_id=ppid or sp.vendor_request_id,
                platform_url=url,
                scheduled_post_id=s_row.id,
            )

            # Resolve drive filename for sheet key BEFORE the session closes.
            video_name = await _resolve_drive_filename(session, s_row.asset_id)

        # Flip the brand's sheet row to `posted` (same update the webhook
        # handler does — reconciliation previously skipped this, leaving
        # rows stuck on `captioned` even though the TikTok post was live).
        if video_name:
            from social_signal.integrations import sheet_tracker
            await sheet_tracker.update_by_video_name(
                s_row.brand_id,
                video_name,
                {
                    "status":     "posted",
                    "posted_at":  pub.published_at.isoformat(timespec="seconds"),
                    "tiktok_url": url or "",
                },
            )

        log.info(
            "scheduler.reconcile_awaiting_webhook_finalized",
            scheduled_post_id=sp.id,
            platform_post_id=pub.platform_post_id,
            via="get_status",
        )


async def _resolve_drive_filename(session, asset_id: str | None) -> str | None:
    """Walk asset → script → signal to find the Drive filename for a sheet key."""
    if not asset_id:
        return None
    from social_signal.db.models import ContentScript, Signal, VideoAsset
    asset = await session.get(VideoAsset, asset_id)
    if not asset or not asset.script_id:
        return None
    cs = await session.get(ContentScript, asset.script_id)
    if not cs or not cs.signal_id:
        return None
    sig = await session.get(Signal, cs.signal_id)
    if not sig or sig.source != "drive":
        return None
    return sig.summary.replace("Drive clip: ", "", 1)


# ---------------------------------------------------------------------------
# 8. poll_orm_mentions — Phase 1 Twitter monitor
# ---------------------------------------------------------------------------

async def _poll_orm_mentions() -> None:
    try:
        from social_signal.orm.monitor import poll_all
        new = await poll_all()
        if new:
            log.info("scheduler.orm_poll", new_mentions=new)
    except Exception as exc:
        log.warning("scheduler.orm_poll_error", error=str(exc))
