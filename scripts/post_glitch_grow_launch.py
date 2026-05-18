#!/usr/bin/env python3
"""AI Marketing Stack launch — post 4 items: brand+founder on X (text) and LinkedIn (PDF).

Text bodies in scripts/ai_marketing_stack_launch/*.txt. LinkedIn carousels are the
two hand-written PDFs generated via carousel_gen.py earlier today. Paths are
hard-coded below — edit if you regenerate.

Usage:
    uv run python scripts/post_ai_marketing_stack_launch.py                     # dry run
    DISPATCH_MODE=live uv run python scripts/post_ai_marketing_stack_launch.py  # live
    DISPATCH_MODE=live uv run python scripts/post_ai_marketing_stack_launch.py --only brand_x
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from social_signal.config import brand_config, settings  # noqa: E402

CONTENT_DIR = Path(__file__).resolve().parent / "ai_marketing_stack_launch"

# LinkedIn carousel PDFs — regenerate via /tmp/gen_ai_marketing_stack_carousels.py
BRAND_PDF = Path(
    "/var/lib/ai-social-media-agent/videos/carousels/"
    "glitch_executor/1949fdec4c3644fa99809d2938739ec4.pdf"
)
FOUNDER_PDF = Path(
    "/var/lib/ai-social-media-agent/videos/carousels/"
    "glitch_founder/4d50526172a6483fb82ea0b87b25533b.pdf"
)

# post_id → (brand_id, platform_key, text_file, pdf_path | None)
POSTS: list[tuple[str, str, str, str, Path | None]] = [
    ("brand_x",        "glitch_executor", "upload_post_x",        "brand_x.txt",        None),
    ("founder_x",      "glitch_founder",  "upload_post_x",        "founder_x.txt",      None),
    ("brand_li",       "glitch_executor", "upload_post_linkedin", "brand_linkedin.txt", BRAND_PDF),
    ("founder_li",     "glitch_founder",  "upload_post_linkedin", "founder_linkedin.txt", FOUNDER_PDF),
]

# Space posts so LinkedIn doesn't rate-limit / de-prioritize same-account
# back-to-back. 30s between X posts is fine; 120s before the LinkedIn
# document posts gives the API the breathing room it wants.
INTER_X_DELAY_S = 30
INTER_LI_DELAY_S = 120


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _load(name: str) -> str:
    p = CONTENT_DIR / name
    if not p.exists():
        _fail(f"missing content file: {p}")
    return p.read_text().strip()


def _resolve_block(brand_id: str, platform_key: str) -> dict:
    cfg = brand_config(brand_id)
    block = cfg.get("platforms", {}).get(platform_key) or {}
    if not block.get("enabled"):
        _fail(f"{brand_id}.{platform_key} is not enabled")
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
    filename: str,
    pdf_path: Path | None,
    dry: bool,
) -> None:
    block = _resolve_block(brand_id, platform_key)
    user = block["user"]
    target = platform_key.replace("upload_post_", "")
    text = _load(filename)

    header = f"── {post_id} → {brand_id} · {platform_key} (user={user}) ──"
    print(f"\n{header}")
    print(_char_note(text, platform_key))
    if pdf_path:
        if not pdf_path.exists():
            _fail(f"pdf missing: {pdf_path}")
        print(f"pdf: {pdf_path.name} ({pdf_path.stat().st_size // 1024} KB)")
    print("─" * len(header))
    print(text)
    print("─" * len(header))

    if dry:
        print("[DRY RUN] skipping API call")
        return

    import upload_post

    api_key = settings().upload_post_api_key
    if not api_key:
        _fail("UPLOAD_POST_API_KEY not set")

    client = upload_post.UploadPostClient(api_key=api_key)

    if pdf_path:
        # LinkedIn document post (PDF carousel). The LinkedIn post caption
        # lives in `description`; `title` is the document's internal name.
        kwargs: dict = {
            "document_path": str(pdf_path),
            "title": f"AI Marketing Stack — launch {post_id}",
            "user": user,
            "description": text,
        }
        if block.get("target_linkedin_page_id"):
            kwargs["target_linkedin_page_id"] = block["target_linkedin_page_id"]
        resp = client.upload_document(**kwargs)
    else:
        # X text post
        kwargs = {"title": text, "user": user, "platforms": [target]}
        resp = client.upload_text(**kwargs)

    print(f"[LIVE] response: {resp}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=[p[0] for p in POSTS])
    parser.add_argument("--no-confirm", action="store_true")
    args = parser.parse_args()

    dry = settings().is_dry_run
    mode = "DRY RUN" if dry else "LIVE"
    print(f"=== AI Marketing Stack launch — {mode} ===")
    print(f"DISPATCH_MODE = {settings().dispatch_mode}")

    targets = [p for p in POSTS if (args.only is None or p[0] == args.only)]
    print(f"Queued: {[p[0] for p in targets]}")

    if not dry and not args.no_confirm:
        print("\nAbout to publish LIVE to production accounts.")
        if input("Type 'POST' to confirm: ").strip() != "POST":
            print("aborted.")
            sys.exit(0)

    for i, (post_id, brand_id, platform_key, filename, pdf_path) in enumerate(targets):
        _post_one(post_id, brand_id, platform_key, filename, pdf_path, dry=dry)

        if dry or i >= len(targets) - 1:
            continue
        next_is_li = targets[i + 1][2] == "upload_post_linkedin"
        delay = INTER_LI_DELAY_S if next_is_li else INTER_X_DELAY_S
        print(f"\nsleeping {delay}s before next post…")
        time.sleep(delay)

    print("\n=== done ===")


if __name__ == "__main__":
    main()
