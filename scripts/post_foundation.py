#!/usr/bin/env python3
"""Foundation launch — post the 4 this project debut posts via Upload-Post.

Reads the 4 post files from scripts/foundation_posts/ and sends them in a safe
sequence with delays. Dry-run by default; set DISPATCH_MODE=live to actually post.

Usage:
    uv run python scripts/post_foundation.py                   # dry run (default)
    DISPATCH_MODE=live uv run python scripts/post_foundation.py   # live
    DISPATCH_MODE=live uv run python scripts/post_foundation.py --only brand_x

The sequence prints exactly what will be sent; in live mode it pauses and asks
you to confirm before hitting the API. X posts assume < 280 chars; LinkedIn
posts use the brand's company-page ID when present.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from glitch_signal.config import brand_config, settings  # noqa: E402

CONTENT_DIR = Path(__file__).resolve().parent / "foundation_posts"

# post_id  →  (brand_id, platform_key, content_file)
POSTS: list[tuple[str, str, str, str]] = [
    # id          brand_id           platform_key             filename
    ("brand_x",        "glitch_executor", "upload_post_x",         "brand_x.txt"),
    ("tejas_x",        "glitch_founder",  "upload_post_x",         "tejas_x.txt"),
    ("brand_linkedin", "glitch_executor", "upload_post_linkedin",  "brand_linkedin.txt"),
    ("tejas_linkedin", "glitch_founder",  "upload_post_linkedin",  "tejas_linkedin.txt"),
]

# Seconds to wait between live posts to avoid rate limiting and give each post
# a clean engagement window.
INTER_POST_DELAY_S = 30


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _load_content(filename: str) -> str:
    path = CONTENT_DIR / filename
    if not path.exists():
        _fail(f"missing content file: {path}")
    return path.read_text().strip()


def _resolve_platform_cfg(brand_id: str, platform_key: str) -> dict:
    cfg = brand_config(brand_id)
    block = cfg.get("platforms", {}).get(platform_key) or {}
    if not block.get("enabled"):
        _fail(f"{brand_id}.{platform_key} is not enabled in brand config")
    if not block.get("user"):
        _fail(f"{brand_id}.{platform_key} missing `user` (Upload-Post profile name)")
    return block


def _char_count_summary(text: str, platform_key: str) -> str:
    n = len(text)
    if platform_key == "upload_post_x":
        warn = " ⚠ over 280" if n > 280 else " ✓"
        return f"{n} chars{warn}"
    if platform_key == "upload_post_linkedin":
        tier = "too short" if n < 800 else "sweet spot" if 1300 <= n <= 2500 else "ok"
        return f"{n} chars ({tier})"
    return f"{n} chars"


def _post_one(post_id: str, brand_id: str, platform_key: str, text: str, dry: bool) -> None:
    block = _resolve_platform_cfg(brand_id, platform_key)
    user = block["user"]
    target = platform_key.replace("upload_post_", "")

    print(f"\n── {post_id} → {brand_id} · {platform_key} (user={user}) ──")
    print(_char_count_summary(text, platform_key))
    print("─" * 60)
    print(text)
    print("─" * 60)

    if dry:
        print("[DRY RUN] skipping API call")
        return

    import upload_post

    api_key = settings().upload_post_api_key
    if not api_key:
        _fail("UPLOAD_POST_API_KEY not set")

    client = upload_post.UploadPostClient(api_key=api_key)
    kwargs: dict = {"title": text, "user": user, "platforms": [target]}

    # LinkedIn company page targeting
    if target == "linkedin" and block.get("target_linkedin_page_id"):
        kwargs["target_linkedin_page_id"] = block["target_linkedin_page_id"]

    resp = client.upload_text(**kwargs)
    print(f"[LIVE] response: {resp}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=[p[0] for p in POSTS],
        help="only send this one post (default: all 4 in sequence)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="skip the interactive 'are you sure' prompt in live mode",
    )
    args = parser.parse_args()

    dry = settings().is_dry_run
    mode = "DRY RUN" if dry else "LIVE"
    print(f"=== Foundation launch — {mode} ===")
    print(f"DISPATCH_MODE = {settings().dispatch_mode}")

    targets = [p for p in POSTS if (args.only is None or p[0] == args.only)]
    print(f"Posts queued: {[p[0] for p in targets]}")

    if not dry and not args.no_confirm:
        print("\nReview above. You are about to publish LIVE to production accounts.")
        answer = input("Type 'POST' to confirm: ")
        if answer.strip() != "POST":
            print("aborted.")
            sys.exit(0)

    for i, (post_id, brand_id, platform_key, filename) in enumerate(targets):
        text = _load_content(filename)
        _post_one(post_id, brand_id, platform_key, text, dry=dry)
        if not dry and i < len(targets) - 1:
            print(f"\nsleeping {INTER_POST_DELAY_S}s before next post…")
            time.sleep(INTER_POST_DELAY_S)

    print("\n=== done ===")


if __name__ == "__main__":
    main()
