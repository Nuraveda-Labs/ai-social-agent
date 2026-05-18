#!/usr/bin/env python3
"""One-time registration of our inbound webhook with Upload-Post.

Usage (from repo root, with .env loaded):
    source .venv/bin/activate
    set -a; source .env; set +a
    python scripts/register_upload_post_webhook.py

This reads:
    UPLOAD_POST_API_KEY
    UPLOAD_POST_WEBHOOK_SECRET   (generate one: python -c "import secrets; print(secrets.token_urlsafe(32))")
    PUBLIC_BASE_URL              (defaults to https://grow.example.com)

…and POSTs to Upload-Post's notifications config endpoint to enable
webhook delivery for all relevant events.

Events subscribed:
  - upload_completed
  - social_account_connected
  - social_account_disconnected
  - social_account_reauth_required

Re-run this whenever you rotate UPLOAD_POST_WEBHOOK_SECRET.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    api_key = os.environ.get("UPLOAD_POST_API_KEY", "").strip()
    secret = os.environ.get("UPLOAD_POST_WEBHOOK_SECRET", "").strip()
    base_url = os.environ.get("PUBLIC_BASE_URL", "https://grow.example.com").rstrip("/")

    if not api_key:
        print("ERROR: UPLOAD_POST_API_KEY is not set in env", file=sys.stderr)
        return 2
    if not secret:
        print(
            "ERROR: UPLOAD_POST_WEBHOOK_SECRET is not set.\n"
            "  Generate one with:\n"
            "    python -c 'import secrets; print(secrets.token_urlsafe(32))'\n"
            "  Save it to .env, then re-run.",
            file=sys.stderr,
        )
        return 2

    webhook_url = f"{base_url}/webhooks/upload_post/{secret}"
    events = [
        "upload_completed",
        "social_account_connected",
        "social_account_disconnected",
        "social_account_reauth_required",
    ]

    # Redact the secret from console output so the registration flow can be
    # pasted into logs / Slack / Telegram without leaking the URL path.
    redacted = webhook_url.replace(secret, "***SECRET***")
    print(f"Registering webhook: {redacted}")
    print(f"Events: {', '.join(events)}")

    # Upload-Post's real webhook config endpoint is
    # POST api.upload-post.com/api/uploadposts/users/notifications.
    # The SDK's `update_notification_config` method POSTs to
    # /uploadposts/notification-config which 404s (stale SDK path).
    # We call the correct endpoint directly. Verified 2026-04-18:
    # GET returns `{"success":true,"notifications":{}}` on a fresh account.
    import requests

    try:
        resp = requests.post(
            "https://api.upload-post.com/api/uploadposts/users/notifications",
            headers={
                "Authorization": f"Apikey {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "webhook_url": webhook_url,
                "webhook_events": events,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"ERROR: HTTP request failed: {exc}", file=sys.stderr)
        return 1

    body = resp.text.replace(secret, "***SECRET***")
    print(f"HTTP {resp.status_code}")
    print(body)
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
