"""Post one queued row from the sheet → Upload-Post → write result back.

Called by the scheduler tick in scheduler/queue.py. Idempotency comes from
the sheet's id column: every call flips the row to posted|failed so the
next tick won't re-fire it.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import PublishedPost, ScheduledPost
from glitch_signal.db.session import _session_factory
from glitch_signal.integrations.google_sheets import update_row_by_key
from glitch_signal.sheet_posting.reader import SHEET_COLUMNS, QueuedPost

log = structlog.get_logger(__name__)


async def post_one(row: QueuedPost) -> tuple[bool, str]:
    """Publish a single queued row. Returns (ok, message).

    On success, updates the sheet with posted_at / post_url / platform_post_id
    and flips status to posted. On failure, flips to failed with the error
    in notes.
    """
    cfg = brand_config(row.brand_id)
    block = (cfg.get("platforms", {}) or {}).get(row.platform) or {}
    user = block.get("user")
    if not block.get("enabled") or not user:
        return await _mark_failed(row, f"{row.brand_id}.{row.platform} not enabled / missing user")

    api_key = settings().upload_post_api_key
    if not api_key:
        return await _mark_failed(row, "UPLOAD_POST_API_KEY unset")

    if settings().is_dry_run:
        log.info(
            "sheet_posting.dry_run",
            row_id=row.id,
            brand_id=row.brand_id,
            platform=row.platform,
        )
        await _write_result(
            row,
            status="posted",
            post_url="https://dry-run.local/fake",
            platform_post_id=f"dry-{row.id[:8]}",
        )
        return True, "[dry-run] marked posted"

    target = row.platform.replace("upload_post_", "")
    text = _augment_body(row, cfg)
    target_linkedin_page_id = block.get("target_linkedin_page_id") if target == "linkedin" else None
    content_type = row.content_type or ("carousel" if target == "linkedin" else "text")

    # Routing policy (April 2026):
    #   Primary publish path = Upload-Post (we have an active monthly
    #   subscription, no per-post marginal cost there).
    #   Fallback for LinkedIn = native Marketing-API path, used only when
    #   Upload-Post itself errors (vendor outage, account lockout, etc.).
    #   Native LinkedIn is otherwise reserved for capabilities Upload-Post
    #   simply doesn't expose — comment read/reply on company-page posts.
    try:
        if content_type == "quote_card":
            # Single designed image (gpt-image-2) + original body as caption
            resp = await _post_as_quote_card(
                api_key=api_key,
                user=user,
                target=target,
                brand_id=row.brand_id,
                body=text,
                target_linkedin_page_id=target_linkedin_page_id,
            )
        elif content_type == "carousel" and target == "linkedin":
            # LinkedIn PDF carousel (multi-slide document post)
            resp = await _post_linkedin_as_carousel(
                api_key=api_key,
                user=user,
                brand_id=row.brand_id,
                body=text,
                target_linkedin_page_id=target_linkedin_page_id,
                row_id=row.id,
            )
        else:
            # Plain text post (default for X, also LinkedIn when content_type=text)
            resp = await asyncio.to_thread(
                _post_to_upload_post,
                api_key=api_key,
                user=user,
                target=target,
                text=text,
                target_linkedin_page_id=target_linkedin_page_id,
            )
    except Exception as exc:
        log.warning("sheet_posting.upload_failed", row_id=row.id, error=str(exc)[:200])
        # Fallback to native LinkedIn API when (a) the row is for LinkedIn
        # and (b) we have a configured token. Lets a vendor outage on
        # Upload-Post not stall LinkedIn publishing.
        if target == "linkedin" and settings().linkedin_access_token:
            try:
                log.info("sheet_posting.linkedin_native_fallback_attempt", row_id=row.id)
                return await _post_via_linkedin_native(
                    row=row, body=text, content_type=content_type,
                )
            except Exception as native_exc:
                log.warning(
                    "sheet_posting.linkedin_native_fallback_failed",
                    row_id=row.id, error=str(native_exc)[:300],
                )
        return await _mark_failed(row, f"upload failed: {exc}")

    # Normalize the response — same handling as the foundation/launch scripts
    platform_post_id, post_url = _extract_post_identifiers(resp, target)
    pending = platform_post_id is None and post_url is None
    status = "posted" if not pending else "posted"  # background-accepted counts as posted

    await _write_result(
        row,
        status=status,
        post_url=post_url or "",
        platform_post_id=platform_post_id or "",
        extra_note=(
            "background upload — status confirmed via webhook / reconcile" if pending else ""
        ),
    )

    # Also write a PublishedPost row so downstream features (comment sweeper,
    # analytics) can find this post. We create a synthetic ScheduledPost
    # first because PublishedPost FKs to it; text posts use the nullable
    # asset_id path added in migration 0006.
    if platform_post_id:
        try:
            await _write_audit_rows(row, platform_post_id, post_url)
        except Exception as exc:
            log.warning("sheet_posting.audit_write_failed", row_id=row.id, error=str(exc)[:200])

    log.info(
        "sheet_posting.posted",
        row_id=row.id,
        brand_id=row.brand_id,
        platform=row.platform,
        platform_post_id=platform_post_id,
    )
    return True, "posted"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_to_upload_post(
    *,
    api_key: str,
    user: str,
    target: str,
    text: str,
    target_linkedin_page_id: str | None,
) -> dict:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    kwargs: dict = {"title": text, "user": user, "platforms": [target]}
    if target == "linkedin" and target_linkedin_page_id:
        kwargs["target_linkedin_page_id"] = target_linkedin_page_id
    return client.upload_text(**kwargs)


async def _post_as_quote_card(
    *,
    api_key: str,
    user: str,
    target: str,
    brand_id: str,
    body: str,
    target_linkedin_page_id: str | None,
) -> dict:
    """Generate a designed image from the sheet body and upload it as an
    image post. Body stays as the caption/description."""
    from glitch_signal.sheet_posting.quote_card import generate_quote_card

    image_path = await generate_quote_card(body=body, brand_id=brand_id)

    def _upload() -> dict:
        import upload_post

        client = upload_post.UploadPostClient(api_key=api_key)
        kwargs: dict = {
            "photos": [str(image_path)],
            "title": body,
            "user": user,
            "platforms": [target],
        }
        if target == "linkedin" and target_linkedin_page_id:
            kwargs["target_linkedin_page_id"] = target_linkedin_page_id
        return client.upload_photos(**kwargs)

    return await asyncio.to_thread(_upload)


async def _post_linkedin_as_carousel(
    *,
    api_key: str,
    user: str,
    brand_id: str,
    body: str,
    target_linkedin_page_id: str | None,
    row_id: str,
) -> dict:
    """Generate a PDF carousel from the sheet body and post via upload_document.

    The body becomes the LinkedIn post description; the PDF is the attached
    document (LinkedIn's highest-engagement native format).
    """
    from glitch_signal.media.carousel_gen import generate_carousel_from_body

    # Pick a sensible CTA link if the body contains one; fall back to org link
    cta_link = "github.com/glitch-exec-labs"
    for token in body.split():
        t = token.strip(".,!?")
        if "github.com/glitch-exec-labs" in t or "example.com" in t:
            cta_link = t
            break

    pdf_path = await generate_carousel_from_body(
        body=body,
        brand_id=brand_id,
        cta_link=cta_link,
    )

    def _upload_doc() -> dict:
        import upload_post

        client = upload_post.UploadPostClient(api_key=api_key)
        kwargs: dict = {
            "document_path": str(pdf_path),
            "title": f"Glitch — {row_id[:8]}",
            "user": user,
            "description": body,
        }
        if target_linkedin_page_id:
            kwargs["target_linkedin_page_id"] = target_linkedin_page_id
        return client.upload_document(**kwargs)

    return await asyncio.to_thread(_upload_doc)


def _extract_post_identifiers(resp: dict, target: str) -> tuple[str | None, str | None]:
    """Pull (platform_post_id, post_url) out of Upload-Post's response shape.

    Handles three shapes:
      - upload_text/photos: {"results": {"<platform>": {...post_id, url...}}}
      - upload_text list:   {"results": [{"platform": "...", ...}]}
      - upload_document:    flat {"request_id", "message"} when queued async,
                            or {"results": {...}} when sync.
    """
    if not isinstance(resp, dict):
        return None, None
    results = resp.get("results") or {}
    block: dict = {}
    if isinstance(results, dict):
        block = results.get(target) or {}
    elif isinstance(results, list) and results:
        block = results[0] if isinstance(results[0], dict) else {}
    pid = (
        block.get("platform_post_id")
        or block.get("post_id")
        or (block.get("url", "").rsplit("/", 1)[-1] if block.get("url") else None)
    )
    url = block.get("post_url") or block.get("url")
    # Fallback for background-accepted document uploads
    if not pid and resp.get("request_id"):
        pid = f"request:{resp['request_id']}"
    return pid, url


async def _write_result(
    row: QueuedPost,
    *,
    status: str,
    post_url: str = "",
    platform_post_id: str = "",
    extra_note: str = "",
) -> None:
    s = settings()
    now = datetime.now(UTC).replace(tzinfo=None)
    updates = {
        "status": status,
        "posted_at": now.isoformat(timespec="seconds"),
        "post_url": post_url,
        "platform_post_id": platform_post_id,
    }
    if extra_note:
        existing = row.notes or ""
        updates["notes"] = (existing + ("; " if existing else "") + extra_note).strip()
    try:
        await update_row_by_key(
            sheet_id=s.glitch_posts_sheet_id,
            worksheet=row.worksheet or s.glitch_posts_worksheet,
            columns=SHEET_COLUMNS,
            key_column="id",
            key_value=row.id,
            updates=updates,
        )
    except Exception as exc:
        log.error("sheet_posting.sheet_update_failed", row_id=row.id, error=str(exc)[:200])


async def _mark_failed(row: QueuedPost, reason: str) -> tuple[bool, str]:
    await _write_result(row, status="failed", extra_note=reason[:180])
    return False, reason


async def _write_audit_rows(
    row: QueuedPost, platform_post_id: str, post_url: str | None
) -> None:
    """Write ScheduledPost + PublishedPost so comment sweeper / analytics see this post."""
    import uuid

    now = datetime.now(UTC).replace(tzinfo=None)
    factory = _session_factory()
    async with factory() as session:
        sp = ScheduledPost(
            id=str(uuid.uuid4()),
            brand_id=row.brand_id,
            asset_id=None,
            script_id=None,
            platform=row.platform,
            scheduled_for=now,
            status="done",
            veto_deadline=now,
            attempts=1,
            last_attempt_at=now,
        )
        session.add(sp)
        await session.flush()

        pp = PublishedPost(
            id=str(uuid.uuid4()),
            brand_id=row.brand_id,
            scheduled_post_id=sp.id,
            platform=row.platform,
            platform_post_id=platform_post_id,
            platform_url=post_url,
            published_at=now,
        )
        session.add(pp)
        await session.commit()


# ---------------------------------------------------------------------------
# Native LinkedIn path (Marketing Developer Platform)
# ---------------------------------------------------------------------------

async def _post_via_linkedin_native(
    *, row: QueuedPost, body: str, content_type: str,
) -> tuple[bool, str]:
    """Publish a sheet row through LinkedIn's direct API instead of Upload-Post.

    Routes by content_type and brand:
      - text       -> /rest/posts (commentary only)
      - quote_card -> not yet implemented natively; raises so caller falls back
      - carousel   -> /rest/documents (initialize → upload PDF) + /rest/posts

    Author URN switches by brand:
      - glitch_founder   -> Tejas's person URN (w_member_social)
      - glitch_executor  -> this project company URN (w_organization_social)

    Returns (ok, msg). Same shape as post_one() so the caller can use it
    transparently. Writes status/posted_at/post_url/platform_post_id back
    to the sheet on success and creates the same audit rows as the
    Upload-Post path.
    """
    from glitch_signal.integrations.linkedin import (
        LinkedInError,
        author_urn_for,
        client_from_settings,
    )

    client = client_from_settings()
    if client is None:
        raise LinkedInError("LinkedInClient not configured")

    author_urn = author_urn_for(row.brand_id)
    if not author_urn:
        raise LinkedInError(
            f"no LinkedIn author URN configured for brand_id={row.brand_id}"
        )

    if content_type == "carousel":
        # Reuse the existing carousel render — gpt-image-2 + img2pdf.
        from glitch_signal.media.carousel_gen import generate_carousel_from_body

        cta_link = "github.com/glitch-exec-labs"
        for token in body.split():
            t = token.strip(".,!?")
            if "github.com/glitch-exec-labs" in t or "example.com" in t:
                cta_link = t
                break

        pdf_path = await generate_carousel_from_body(
            body=body, brand_id=row.brand_id, cta_link=cta_link,
        )
        upload_url, document_urn = await client.register_document_upload(author_urn)
        await client.upload_pdf(upload_url, str(pdf_path))
        # LinkedIn needs a moment to process the PDF before /rest/posts
        # accepts the document URN. AVAILABLE typically lands in 5-15s.
        await client.wait_for_document(document_urn, timeout_s=120)
        result = await client.post_document(
            author_urn=author_urn,
            commentary=body,
            document_urn=document_urn,
            title=f"Glitch — {row.id[:8]}",
        )
    elif content_type == "text":
        result = await client.post_text(author_urn=author_urn, commentary=body)
    else:
        # quote_card: image post via /rest/posts not yet implemented; let
        # the caller fall back to Upload-Post for now.
        raise LinkedInError(
            f"native LinkedIn path doesn't yet support content_type={content_type!r}"
        )

    log.info(
        "sheet_posting.linkedin_native_posted",
        row_id=row.id,
        brand_id=row.brand_id,
        author_urn=author_urn,
        post_urn=result.post_urn,
    )

    await _write_result(
        row,
        status="posted",
        post_url=result.post_url,
        platform_post_id=result.post_urn,
    )
    try:
        await _write_audit_rows(row, result.post_urn, result.post_url)
    except Exception as exc:
        log.warning(
            "sheet_posting.linkedin_native_audit_write_failed",
            row_id=row.id, error=str(exc)[:200],
        )
    return True, "posted (linkedin native)"


# ---------------------------------------------------------------------------
# Body augmentation — auto-append hashtags + github link when missing
# ---------------------------------------------------------------------------

def _augment_body(row: QueuedPost, cfg: dict) -> str:
    """Append per-brand hashtags + repo link if the operator's body didn't
    include them. Idempotent: a body that already has the tags / link is
    returned unchanged.

    Hashtag source (in priority order):
      - cfg["platforms"][row.platform]["hashtags"]    — platform-specific
      - cfg["default_hashtags"]                       — brand-wide

    Repo link source:
      - cfg["platforms"][row.platform]["default_repo_link"]
      - cfg["default_repo_link"]
    """
    body = (row.body or "").strip()
    if not body:
        return body

    block = (cfg.get("platforms", {}) or {}).get(row.platform) or {}
    hashtags = list(block.get("hashtags") or cfg.get("default_hashtags") or [])
    repo_link = block.get("default_repo_link") or cfg.get("default_repo_link") or ""

    additions: list[str] = []

    if repo_link:
        # Heuristic: only append if no link to the same domain is in the body.
        domain = repo_link.replace("https://", "").replace("http://", "").split("/")[0]
        if domain and domain not in body:
            additions.append(repo_link)

    if hashtags:
        # Detect any tag already present (case-insensitive).
        body_lower = body.lower()
        missing = [t for t in hashtags if t.lower() not in body_lower]
        if missing:
            additions.append(" ".join(missing))

    if not additions:
        return body

    # Two newlines if body ends with a paragraph; one newline if a single line.
    sep = "\n\n" if "\n\n" in body else "\n"
    return f"{body}{sep}{' '.join(additions)}"
