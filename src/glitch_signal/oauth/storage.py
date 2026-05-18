"""PlatformAuth CRUD with encryption at rest.

All plaintext tokens stay in memory only. Anything persisted to Postgres
goes through encrypt()/decrypt() from glitch_signal.crypto.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import select

from glitch_signal.crypto import decrypt, encrypt
from glitch_signal.db.models import PlatformAuth
from glitch_signal.db.session import _session_factory


@dataclass
class PlainAuth:
    """In-memory, plaintext view of a PlatformAuth row."""
    id: str
    brand_id: str
    platform: str
    account_identifier: str | None
    access_token: str
    refresh_token: str | None
    access_token_expires_at: datetime | None
    scopes: list[str]
    status: str


async def upsert(
    *,
    brand_id: str,
    platform: str,
    account_identifier: str | None,
    access_token: str,
    refresh_token: str | None,
    access_token_expires_at: datetime | None,
    scopes: list[str],
    raw_provider_response: dict | None = None,
) -> str:
    """Insert or update the row for (brand_id, platform, account_identifier).

    Returns the PlatformAuth.id.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PlatformAuth).where(
                PlatformAuth.brand_id == brand_id,
                PlatformAuth.platform == platform,
                PlatformAuth.account_identifier == account_identifier,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            row = PlatformAuth(
                id=str(uuid.uuid4()),
                brand_id=brand_id,
                platform=platform,
                account_identifier=account_identifier,
                access_token_enc=encrypt(access_token),
                refresh_token_enc=encrypt(refresh_token) if refresh_token else None,
                access_token_expires_at=access_token_expires_at,
                scopes=json.dumps(scopes),
                status="active",
                raw_provider_response=json.dumps(raw_provider_response or {}),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.access_token_enc = encrypt(access_token)
            if refresh_token is not None:
                row.refresh_token_enc = encrypt(refresh_token)
            row.access_token_expires_at = access_token_expires_at
            row.scopes = json.dumps(scopes)
            row.status = "active"
            row.raw_provider_response = json.dumps(raw_provider_response or {})
            row.updated_at = now
            session.add(row)

        await session.commit()
        return row.id


async def get(brand_id: str, platform: str) -> PlainAuth | None:
    """Return the active auth record for this (brand, platform), decrypted.

    If multiple rows exist for different accounts, returns the most recently
    updated active one.
    """
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PlatformAuth)
            .where(
                PlatformAuth.brand_id == brand_id,
                PlatformAuth.platform == platform,
                PlatformAuth.status == "active",
            )
            .order_by(PlatformAuth.updated_at.desc())
            .limit(1)
        )
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


async def mark_needs_reauth(brand_id: str, platform: str) -> None:
    factory = _session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PlatformAuth).where(
                PlatformAuth.brand_id == brand_id,
                PlatformAuth.platform == platform,
            )
        )
        for row in result.scalars().all():
            row.status = "needs_reauth"
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(row)
        await session.commit()
