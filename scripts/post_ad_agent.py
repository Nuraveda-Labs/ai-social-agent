#!/usr/bin/env python3
"""One-off posting script — Ad Agent HITL reveal.

Posts the Ad Agent Telegram proposal story to Tejas X (text) and Tejas LinkedIn
(text + image). The LinkedIn post carries the Telegram screenshot so readers see
the exact proposal format.

Usage:
    uv run python scripts/post_ad_agent.py                      # dry run
    DISPATCH_MODE=live uv run python scripts/post_ad_agent.py   # live
    DISPATCH_MODE=live uv run python scripts/post_ad_agent.py --only tejas_x

Files expected:
    scripts/ad_agent_posts/tejas_x.txt       — X post text (< 280 chars)
    scripts/ad_agent_posts/tejas_linkedin.txt — LinkedIn post text
    scripts/ad_agent_posts/proposal.png      — Telegram screenshot of the proposal
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from glitch_signal.config import brand_config, settings  # noqa: E402

CONTENT_DIR = Path(__file__).resolve().parent / "ad_agent_posts"
IMAGE_PATH = CONTENT_DIR / "proposal.png"

# post_id → (brand_id, platform_key, filename, with_image)
POSTS: list[tuple[str, str, str, str, bool]] = [
    ("tejas_x",        "glitch_founder", "upload_post_x",        "tejas_x.txt",        False),
    ("tejas_linkedin", "glitch_founder", "upload_post_linkedin", "tejas_linkedin.txt", True),
]

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
        _fail(f"{brand_id}.{platform_key} missing `user`")
    return block


def _char_note(text: str, platform_key: str) -> str:
    n = len(text)
    if platform_key == "upload_post_x":
        return f"{n} chars" + (" ⚠ over 280" if n > 280 else " ✓")
    return f"{n} chars"


def _post_one(
    post_id: str,
    brand_id: str,
    platform_key: str,
    text: str,
    with_image: bool,
    dry: bool,
) -> None:
    block = _resolve_platform_cfg(brand_id, platform_key)
    user = block["user"]
    target = platform_key.replace("upload_post_", "")

    print(f"\n── {post_id} → {brand_id} · {platform_key} (user={user}) ──")
    print(_char_note(text, platform_key))
    if with_image:
        if not IMAGE_PATH.exists():
            _fail(
                f"content_type=image requested but {IMAGE_PATH} is missing. "
                "Save the Telegram screenshot to that exact path and retry."
            )
        size_kb = IMAGE_PATH.stat().st_size / 1024
        print(f"image: {IMAGE_PATH.name} ({size_kb:.1f} KB)")
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
    if target == "linkedin" and block.get("target_linkedin_page_id"):
        kwargs["target_linkedin_page_id"] = block["target_linkedin_page_id"]

    if with_image:
        resp = client.upload_photos(
            photos=[str(IMAGE_PATH)],
            **kwargs,
        )
    else:
        resp = client.upload_text(**kwargs)

    print(f"[LIVE] response: {resp}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=[p[0] for p in POSTS])
    parser.add_argument("--no-confirm", action="store_true")
    args = parser.parse_args()

    dry = settings().is_dry_run
    mode = "DRY RUN" if dry else "LIVE"
    print(f"=== Ad Agent reveal — {mode} ===")
    print(f"DISPATCH_MODE = {settings().dispatch_mode}")

    targets = [p for p in POSTS if (args.only is None or p[0] == args.only)]
    print(f"Posts queued: {[p[0] for p in targets]}")

    if not dry and not args.no_confirm:
        print("\nLIVE mode. You are publishing to production accounts.")
        if input("Type 'POST' to confirm: ").strip() != "POST":
            print("aborted.")
            sys.exit(0)

    for i, (post_id, brand_id, platform_key, filename, with_image) in enumerate(targets):
        text = _load_content(filename)
        _post_one(post_id, brand_id, platform_key, text, with_image, dry=dry)
        if not dry and i < len(targets) - 1:
            print(f"\nsleeping {INTER_POST_DELAY_S}s…")
            time.sleep(INTER_POST_DELAY_S)

    print("\n=== done ===")


if __name__ == "__main__":
    main()
