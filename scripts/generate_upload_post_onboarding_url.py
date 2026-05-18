#!/usr/bin/env python3
"""Generate a 1-click Upload-Post account-link URL for a brand.

Usage:
  python scripts/generate_upload_post_onboarding_url.py \\
      --user NewBrand \\
      --platforms tiktok,instagram \\
      [--redirect-url https://example.com/connected] \\
      [--title "Connect your socials to NewBrand"] \\
      [--description "We'll post on your behalf — you can disconnect anytime."]

Reads UPLOAD_POST_API_KEY from the environment / .env. Prints the URL to
stdout so it can be piped, echoed to Telegram, etc.

Prerequisite: the `--user` profile must exist on Upload-Post. Create one
first with the dashboard or:

  python -c "import upload_post, os; \\
    print(upload_post.UploadPostClient(api_key=os.environ['UPLOAD_POST_API_KEY']) \\
      .create_user('NewBrand'))"
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def _parse_argv(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", required=True, help="Upload-Post profile username")
    p.add_argument(
        "--platforms",
        default="",
        help="Comma-separated platforms to show for linking "
             "(tiktok,instagram,youtube,linkedin,facebook,x,threads,pinterest,bluesky,reddit). "
             "Empty = show all.",
    )
    p.add_argument("--redirect-url", default=None, help="URL to redirect to after linking")
    p.add_argument("--redirect-button-text", default=None)
    p.add_argument("--logo-image", default=None)
    p.add_argument("--title", dest="connect_title", default=None)
    p.add_argument("--description", dest="connect_description", default=None)
    p.add_argument(
        "--show-calendar", action="store_true",
        help="Include the Upload-Post scheduling calendar on the connect page",
    )
    return p.parse_args(argv)


def _platforms_list(raw: str) -> list[str] | None:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return parts or None


async def _main(argv: list[str]) -> int:
    args = _parse_argv(argv)

    from glitch_signal.onboarding.upload_post import generate_onboarding_url

    try:
        url = await generate_onboarding_url(
            username=args.user,
            platforms=_platforms_list(args.platforms),
            redirect_url=args.redirect_url,
            redirect_button_text=args.redirect_button_text,
            logo_image=args.logo_image,
            connect_title=args.connect_title,
            connect_description=args.connect_description,
            show_calendar=args.show_calendar or None,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Print the URL on its own line so shell wrappers can capture it.
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
