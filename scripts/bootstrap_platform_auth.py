"""Seed platform_auth rows from .env for X (both brands) + LinkedIn (founder).

Run once after operator pastes fresh OAuth tokens. From then on the runtime
reads auth from platform_auth (encrypted at rest) and refreshes via
oauth.refresh.get_with_auto_refresh() — .env tokens become bootstrap-only.

For X, expires_at is set to now + 7200s since the .env values are minted
within minutes of running this script. For LinkedIn, expires_at defaults
to None (60-day lifetime, refresh handled lazily on near-expiry).

Usage:
    set -a; source .env; set +a
    python scripts/bootstrap_platform_auth.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from glitch_signal.oauth import storage


async def seed_x(brand_id: str, prefix: str) -> None:
    """Pull X tokens from .env using the prefix (X_BRAND_ / X_FOUNDER_)
    and upsert into platform_auth as platform="x"."""
    access = os.environ.get(f"{prefix}ACCESS_TOKEN", "").strip()
    refresh = os.environ.get(f"{prefix}REFRESH_TOKEN", "").strip()
    user_id = os.environ.get(f"{prefix}USER_ID", "").strip()
    if not access or not refresh:
        print(f"  SKIP {brand_id}/x — missing {prefix}ACCESS_TOKEN/REFRESH_TOKEN")
        return
    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)
    auth_id = await storage.upsert(
        brand_id=brand_id,
        platform="x",
        account_identifier=user_id or None,
        access_token=access,
        refresh_token=refresh,
        access_token_expires_at=expires_at,
        scopes=[
            "tweet.read", "tweet.write", "users.read",
            "follows.read", "follows.write",
            "like.read", "like.write",
            "dm.read", "dm.write",
            "offline.access",
        ],
        raw_provider_response={"source": "bootstrap_platform_auth.py"},
    )
    print(f"  ok  {brand_id}/x  account_identifier={user_id}  id={auth_id}")


async def seed_linkedin_founder() -> None:
    access = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
    refresh = os.environ.get("LINKEDIN_REFRESH_TOKEN", "").strip()
    person_urn = os.environ.get("LINKEDIN_FOUNDER_PERSON_URN", "").strip()
    if not access or not refresh:
        print("  SKIP glitch_founder/linkedin — missing tokens")
        return
    # LinkedIn tokens are 60 days; we don't know the exact mint time so
    # just assume now + 55 days as a safe lower bound. The refresh layer
    # will refresh lazily anyway.
    expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=55)
    auth_id = await storage.upsert(
        brand_id="glitch_founder",
        platform="linkedin",
        account_identifier=person_urn or None,
        access_token=access,
        refresh_token=refresh,
        access_token_expires_at=expires_at,
        scopes=[
            "openid", "profile", "email",
            "w_member_social",
            "w_organization_social", "r_organization_social",
            "rw_organization_admin", "r_organization_admin",
            "r_ads", "rw_ads", "r_ads_reporting",
        ],
        raw_provider_response={"source": "bootstrap_platform_auth.py"},
    )
    print(f"  ok  glitch_founder/linkedin  account_identifier={person_urn}  id={auth_id}")


async def main() -> None:
    print("Seeding platform_auth from .env ...")
    await seed_x("glitch_executor", "X_BRAND_")
    await seed_x("glitch_founder", "X_FOUNDER_")
    await seed_linkedin_founder()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
