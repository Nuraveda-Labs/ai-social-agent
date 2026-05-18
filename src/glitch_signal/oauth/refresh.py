"""Concurrency-safe access-token refresh on top of PlatformAuth storage.

Why this exists:
  X (Twitter) issues 2-hour access tokens with refresh-token rotation —
  every refresh invalidates the previous refresh token. If two callers race
  to refresh the same row, one's call succeeds and the other's refresh
  token is now permanently dead. We protect against that with a row-level
  SELECT … FOR UPDATE during the refresh window.

  LinkedIn has 60-day tokens and rotates more leniently, so this matters
  less, but the same pattern applies for any provider.

Public API:
  await get_with_auto_refresh(
      brand_id="glitch_executor",
      platform="x",
      refresh_callback=async_fn(refresh_token) -> RefreshedTokens,
      safety_margin_s=60,
  ) -> PlainAuth

The callback is provider-specific (X and LinkedIn hit different endpoints),
so each provider supplies its own. This module owns only the locking and
persistence; it never knows the OAuth endpoint shape.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlmodel import select

from glitch_signal.crypto import decrypt, encrypt
from glitch_signal.db.models import PlatformAuth
from glitch_signal.db.session import _session_factory
from glitch_signal.oauth.storage import PlainAuth

log = structlog.get_logger(__name__)


@dataclass
class RefreshedTokens:
    """What a provider's refresh callback must return."""
    access_token: str
    refresh_token: str | None      # None if provider didn't rotate
    expires_in_s: int               # seconds until access token expires
    scopes: list[str] | None = None
    raw_response: dict | None = None


# Per-process lock keyed on (brand_id, platform). Stops one Python process
# from racing itself before we even hit Postgres. The DB lock is the real
# defense across processes, but this saves a few wasted refresh round-trips
# when many coroutines all hit a near-expiry token at once.
_PROCESS_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _process_lock_for(brand_id: str, platform: str) -> asyncio.Lock:
    key = (brand_id, platform)
    lock = _PROCESS_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROCESS_LOCKS[key] = lock
    return lock


async def get_with_auto_refresh(
    *,
    brand_id: str,
    platform: str,
    refresh_callback: Callable[[str], Awaitable[RefreshedTokens]],
    safety_margin_s: int = 60,
    account_identifier: str | None = None,
) -> PlainAuth:
    """Fetch the auth row; refresh + persist if access token is near expiry.

    Concurrency-safe across coroutines (asyncio.Lock per key) and across
    processes (Postgres row lock during the refresh write).

    Raises RuntimeError if no row exists or the refresh callback fails.
    """
    proc_lock = _process_lock_for(brand_id, platform)
    async with proc_lock:
        # Re-read inside the lock so we don't refresh based on a stale view
        # if another coroutine just rotated us a fresh token.
        auth = await _read_auth(brand_id, platform, account_identifier)
        if auth is None:
            raise RuntimeError(
                f"no PlatformAuth row for brand_id={brand_id} platform={platform}"
            )

        if not _needs_refresh(auth, safety_margin_s):
            return auth

        if not auth.refresh_token:
            raise RuntimeError(
                f"PlatformAuth {brand_id}/{platform} has no refresh_token; "
                "operator must re-authorize"
            )

        # Hot path — actually refresh + write back atomically.
        log.info(
            "oauth.refresh.starting",
            brand_id=brand_id, platform=platform,
            expires_at=str(auth.access_token_expires_at),
        )
        try:
            new_tokens = await refresh_callback(auth.refresh_token)
        except Exception as exc:
            log.error(
                "oauth.refresh.callback_failed",
                brand_id=brand_id, platform=platform, error=str(exc)[:300],
            )
            raise

        return await _persist_refreshed(
            auth=auth,
            new_tokens=new_tokens,
        )


def _needs_refresh(auth: PlainAuth, safety_margin_s: int) -> bool:
    """True iff the access token is missing, has no expiry, or expires
    within `safety_margin_s` seconds."""
    if not auth.access_token:
        return True
    if auth.access_token_expires_at is None:
        # No recorded expiry — assume long-lived (e.g. LinkedIn).
        return False
    cutoff = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=safety_margin_s)
    return auth.access_token_expires_at <= cutoff


async def _read_auth(
    brand_id: str, platform: str, account_identifier: str | None,
) -> PlainAuth | None:
    """Decrypt + return the most recently updated active auth row. Same
    shape as oauth.storage.get(), inlined here so we can also issue
    SELECT FOR UPDATE in the same transaction during refresh persistence.
    """
    factory = _session_factory()
    async with factory() as session:
        stmt = select(PlatformAuth).where(
            PlatformAuth.brand_id == brand_id,
            PlatformAuth.platform == platform,
            PlatformAuth.status == "active",
        )
        if account_identifier is not None:
            stmt = stmt.where(PlatformAuth.account_identifier == account_identifier)
        stmt = stmt.order_by(PlatformAuth.updated_at.desc()).limit(1)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return None
        return PlainAuth(
            id=row.id,
            brand_id=row.brand_id,
            platform=row.platform,
            account_identifier=row.account_identifier,
            access_token=decrypt(row.access_token_enc),
            refresh_token=decrypt(row.refresh_token_enc) if row.refresh_token_enc else None,
            access_token_expires_at=row.access_token_expires_at,
            scopes=json.loads(row.scopes or "[]"),
            status=row.status,
        )


async def _persist_refreshed(
    *, auth: PlainAuth, new_tokens: RefreshedTokens,
) -> PlainAuth:
    """Write the refreshed token back to Postgres under a row-level lock.

    The lock prevents two processes from racing through refresh at the same
    time and silently invalidating each other's refresh tokens (X rotates
    refresh tokens; one survives, the other becomes dead).
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    new_expires_at = now + timedelta(seconds=new_tokens.expires_in_s)

    factory = _session_factory()
    async with factory() as session:
        # SELECT FOR UPDATE so a concurrent process refreshing the same row
        # blocks here instead of also calling the provider in parallel.
        result = await session.execute(
            select(PlatformAuth)
            .where(PlatformAuth.id == auth.id)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise RuntimeError(f"PlatformAuth {auth.id} disappeared during refresh")

        # Refresh-token rotation: keep the old one if the provider didn't
        # send a new one (LinkedIn behavior on some refreshes).
        row.access_token_enc = encrypt(new_tokens.access_token)
        if new_tokens.refresh_token:
            row.refresh_token_enc = encrypt(new_tokens.refresh_token)
        row.access_token_expires_at = new_expires_at
        if new_tokens.scopes is not None:
            row.scopes = json.dumps(new_tokens.scopes)
        if new_tokens.raw_response is not None:
            row.raw_provider_response = json.dumps(new_tokens.raw_response)
        row.status = "active"
        row.updated_at = now

        session.add(row)
        await session.commit()

    log.info(
        "oauth.refresh.persisted",
        brand_id=auth.brand_id, platform=auth.platform,
        new_expires_at=str(new_expires_at),
        rotated_refresh=bool(new_tokens.refresh_token),
    )

    return PlainAuth(
        id=auth.id,
        brand_id=auth.brand_id,
        platform=auth.platform,
        account_identifier=auth.account_identifier,
        access_token=new_tokens.access_token,
        refresh_token=new_tokens.refresh_token or auth.refresh_token,
        access_token_expires_at=new_expires_at,
        scopes=new_tokens.scopes if new_tokens.scopes is not None else auth.scopes,
        status="active",
    )
