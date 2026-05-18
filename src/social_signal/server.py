"""FastAPI application for AI Social Agent.

Endpoints:
  GET  /healthz                    — liveness
  POST /jobs/scout                 — trigger Scout node manually
  POST /jobs/assemble/{script_id}  — trigger VideoAssembler for a script

Approval / HITL surface lives in Discord now (glitch-discord-bot service +
in-process plugin). The Telegram bot was retired 2026-05-01.
"""
from __future__ import annotations

import asyncio
import pathlib

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import select

from social_signal import __version__
from social_signal.config import brand_ids, settings
from social_signal.crypto import verify_state_token
from social_signal.db.models import ScheduledPost, VideoJob
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

app = FastAPI(
    title="AI Social Agent",
    version=__version__,
    description="Autonomous social video + ORM agent for this project.",
)

_graph = None


@app.on_event("startup")
async def startup() -> None:
    global _graph

    # Build LangGraph
    from social_signal.agent.graph import get_graph
    _graph = get_graph()

    # Start scheduler
    from social_signal.scheduler.queue import start as start_scheduler
    start_scheduler()

    log.info("social_signal.started", version=__version__, port=3111)


@app.on_event("shutdown")
async def shutdown() -> None:
    from social_signal.scheduler.queue import stop as stop_scheduler
    stop_scheduler()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict:
    factory = _session_factory()
    async with factory() as session:
        pending_veto_r = await session.execute(
            select(ScheduledPost).where(ScheduledPost.status == "pending_veto")
        )
        queued_r = await session.execute(
            select(ScheduledPost).where(ScheduledPost.status == "queued")
        )
        dispatching_r = await session.execute(
            select(VideoJob).where(VideoJob.status == "dispatched")
        )

    return {
        "status": "ok",
        "service": "social-signal",
        "version": __version__,
        "dispatch_mode": settings().dispatch_mode,
        "queue": {
            "pending_veto": len(pending_veto_r.scalars().all()),
            "queued_to_publish": len(queued_r.scalars().all()),
            "shots_in_flight": len(dispatching_r.scalars().all()),
        },
    }


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

@app.post("/jobs/scout")
async def job_scout(request: Request) -> dict:
    """Trigger a Scout run manually. Optionally pass {signal_id, platform} to run full pipeline."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    state = {
        "signal_id": body.get("signal_id", ""),
        "platform": body.get("platform", "youtube_shorts"),
        "retry_count": 0,
    }
    asyncio.create_task(_graph.ainvoke(state))
    return {"ok": True, "message": "Scout triggered in background"}


@app.post("/jobs/assemble/{script_id}")
async def job_assemble(script_id: str) -> dict:
    """Manually trigger VideoAssembler for a script where all shots are done."""
    from social_signal.scheduler.queue import _trigger_assembler
    asyncio.create_task(_trigger_assembler(script_id))
    return {"ok": True, "script_id": script_id}


@app.post("/jobs/drive_scout")
async def job_drive_scout(request: Request, brand: str) -> dict:
    """Trigger the drive_footage pipeline for a brand.

    Reads the brand's drive_folder_id from config, discovers new video files,
    downloads them, and runs drive_scout → caption_writer → publisher
    for the first new signal. Returns immediately after dispatching.
    """
    from social_signal.config import brand_config, brand_ids

    if brand not in brand_ids():
        raise HTTPException(status_code=400, detail=f"Unknown brand: {brand!r}")

    cfg = brand_config(brand)
    if cfg.get("content_source") != "drive_footage":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Brand {brand!r} content_source is {cfg.get('content_source')!r}; "
                "drive_scout only runs for brands with content_source=drive_footage"
            ),
        )

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    state = {
        "brand_id": brand,
        "content_source": "drive_footage",
        "signal_id": body.get("signal_id", ""),
        "platform": body.get("platform", "tiktok"),
        "retry_count": 0,
    }
    asyncio.create_task(_graph.ainvoke(state))
    return {
        "ok": True,
        "brand": brand,
        "message": "drive_scout dispatched in background",
    }


# ---------------------------------------------------------------------------
# OAuth — TikTok Content Posting API
# ---------------------------------------------------------------------------
# These routes are exposed at grow.example.com/oauth/tiktok/* via the
# nginx proxy config on that host (see README). The redirect_uri registered
# on the TikTok developer app must point at /oauth/tiktok/callback on this
# same host.

@app.get("/oauth/tiktok/start")
async def oauth_tiktok_start(brand: str) -> RedirectResponse:
    if brand not in brand_ids():
        raise HTTPException(status_code=400, detail=f"Unknown brand: {brand!r}")

    from social_signal.oauth.tiktok import build_authorize_url
    try:
        url = build_authorize_url(brand)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    log.info("oauth.tiktok.start", brand=brand)
    return RedirectResponse(url=url, status_code=302)


@app.get("/oauth/tiktok/callback")
async def oauth_tiktok_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    if error:
        log.warning("oauth.tiktok.callback_error", error=error, desc=error_description)
        return HTMLResponse(
            _html_page(
                "TikTok authorization cancelled",
                f"Provider returned error: <code>{error}</code><br>"
                f"{error_description or ''}",
            ),
            status_code=400,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    from social_signal.oauth import tiktok as tiktok_oauth

    try:
        brand_id = tiktok_oauth.parse_state(state)
    except ValueError as exc:
        log.warning("oauth.tiktok.bad_state", error=str(exc))
        raise HTTPException(status_code=400, detail=f"Invalid state: {exc}") from exc

    if brand_id not in brand_ids():
        raise HTTPException(status_code=400, detail=f"Unknown brand: {brand_id!r}")

    try:
        tokens = await tiktok_oauth.exchange_code_for_tokens(code)
        row_id = await tiktok_oauth.persist_tokens(brand_id, tokens)
    except Exception as exc:
        log.exception("oauth.tiktok.exchange_failed", brand=brand_id)
        return HTMLResponse(
            _html_page(
                "TikTok connection failed",
                f"Token exchange failed: <code>{exc}</code>",
            ),
            status_code=502,
        )

    log.info(
        "oauth.tiktok.connected",
        brand=brand_id,
        open_id=tokens.get("open_id"),
        scopes=tokens.get("scope"),
        platform_auth_id=row_id,
    )
    return HTMLResponse(
        _html_page(
            "TikTok connected",
            f"Brand <code>{brand_id}</code> is now connected to TikTok "
            f"(open_id <code>{tokens.get('open_id')}</code>, scopes "
            f"<code>{tokens.get('scope')}</code>). You can close this tab.",
        )
    )


# ---------------------------------------------------------------------------
# Media-serve — HMAC-signed short-lived URL for vendor fetch
# ---------------------------------------------------------------------------
# Used by platforms/zernio.py. The token is an HMAC-signed JSON payload
# that encodes the exact filesystem path + a 1-hour TTL, so a token
# issued for file A can't be used to fetch file B, and leaked tokens
# expire quickly. Only paths under VIDEO_STORAGE_PATH are served — the
# endpoint refuses absolute paths outside that tree.

_MEDIA_KIND = "media"


@app.post("/webhooks/upload_post/{secret}")
async def upload_post_webhook(secret: str, request: Request) -> dict:
    """Inbound Upload-Post webhook.

    Upload-Post does not sign webhook bodies, so the URL path `secret` is
    the only access control. Set UPLOAD_POST_WEBHOOK_SECRET to a long
    random string and register `https://.../webhooks/upload_post/<secret>`
    with Upload-Post (see scripts/register_upload_post_webhook.py).

    The endpoint always returns 200 on success even when the event was
    unhandled / unknown — returning an error status would cause
    Upload-Post to retry, which produces log noise and no useful state
    change on our side.
    """
    expected = settings().upload_post_webhook_secret
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="upload_post webhook is not configured on this instance",
        )
    # Constant-time string comparison — the secret lives in the URL path
    # and could end up in logs, but using `==` directly would leak length
    # via timing. Use hmac.compare_digest for safety.
    import hmac as _hmac
    if not _hmac.compare_digest(secret, expected):
        log.warning("upload_post.webhook.bad_secret", len=len(secret))
        raise HTTPException(status_code=403, detail="invalid secret")

    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("upload_post.webhook.bad_json", error=str(exc))
        raise HTTPException(status_code=400, detail="invalid json") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected json object")

    from social_signal.webhooks.upload_post import dispatch
    result = await dispatch(payload)
    return result


def _resolve_media_path(token: str) -> pathlib.Path:
    """Validate signed media token and return the on-disk path.

    Shared by GET and HEAD handlers. Raises HTTPException on any failure
    so both verbs return identical status codes to the caller.
    """
    try:
        payload = verify_state_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=f"Invalid media token: {exc}") from exc

    if payload.get("k") != _MEDIA_KIND:
        raise HTTPException(status_code=403, detail="Token is not a media token")

    raw_path = payload.get("p")
    if not raw_path:
        raise HTTPException(status_code=400, detail="Token missing path")

    # Resolve + confinement check: only paths under VIDEO_STORAGE_PATH are
    # served. Prevents traversal even if a token is crafted maliciously.
    path = pathlib.Path(raw_path).resolve()
    storage_root = pathlib.Path(settings().video_storage_path).resolve()
    try:
        path.relative_to(storage_root)
    except ValueError as exc:
        log.warning("media.fetch.path_escape_attempt", path=str(path))
        raise HTTPException(status_code=403, detail="Path outside media root") from exc

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")

    return path


@app.get("/media/fetch")
async def media_fetch(token: str) -> FileResponse:
    path = _resolve_media_path(token)
    log.info("media.fetch.served", path=str(path), bytes=path.stat().st_size)
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        filename=path.name,
    )


@app.head("/media/fetch")
async def media_fetch_head(token: str) -> Response:
    """HEAD pre-flight for vendors that validate media URLs before ingest.

    Buffer (and likely other partners) issue a HEAD request to `/media/fetch`
    before accepting the URL into a post. A 405 on HEAD gets reported back
    as "URL not accessible" and the post is rejected. This mirrors the GET
    validation (signed-token check + path confinement) without streaming
    bytes — just returns the headers the vendor needs to proceed.
    """
    path = _resolve_media_path(token)
    size = path.stat().st_size
    return Response(
        status_code=200,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Accept-Ranges": "bytes",
        },
    )


def _html_page(title: str, body_html: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        "<style>body{font-family:ui-sans-serif,system-ui;background:#0a0a0f;color:#e6e6e6;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".card{max-width:560px;padding:32px;border:1px solid #222;border-radius:12px;"
        "background:#111}h1{margin:0 0 12px;font-size:18px;color:#00ff88}"
        "code{background:#222;padding:2px 6px;border-radius:4px}</style>"
        f"</head><body><div class=\"card\"><h1>{title}</h1><p>{body_html}</p></div></body></html>"
    )
