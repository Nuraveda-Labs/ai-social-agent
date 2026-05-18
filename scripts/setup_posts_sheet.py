#!/usr/bin/env python3
"""Create (or refresh) the Glitch posts sheet and seed it with drafted rows.

The sheet is the single source of truth for queued text posts. Scheduler
tick reads it, picks the next due row (pacing-respecting), posts via
Upload-Post, writes the result back.

Workflow:
  1. Run this script ONCE to create the sheet. It prints the URL + ID.
  2. Paste the ID into .env as GLITCH_POSTS_SHEET_ID.
  3. Share the sheet with your personal Google account (the SA already has
     access since it created the sheet). Optional but lets you edit on
     the fly from a browser.
  4. Restart the agent service. Scheduler tick begins firing queued rows
     within ~5 min, respecting the 4h per-(brand,platform) interval and
     2/day cap configured in settings.

Re-running the script after setup: if GLITCH_POSTS_SHEET_ID is already set,
the script refuses to re-seed — you probably don't want to wipe or
duplicate your posting calendar. Pass --force to append fresh drafts on
top of the existing queue.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import asyncio  # noqa: E402

from glitch_signal.config import settings  # noqa: E402
from glitch_signal.integrations.google_sheets import (  # noqa: E402
    _service,
    append_row,
    ensure_header,
)
from glitch_signal.sheet_posting.reader import SHEET_COLUMNS  # noqa: E402

# ---------------------------------------------------------------------------
# Seed content — drafted against the actual work Tejas has shipped.
# Brand (glitch_executor)  = "we", technical, direct.
# Founder (glitch_founder) = "I", first-person, lesson/feeling.
# No fabricated metrics. Char limits enforced on X (≤ 280).
# ---------------------------------------------------------------------------

_GH_LINK = "github.com/glitch-exec-labs"

SEED_POSTS: list[dict[str, str]] = [
    # ====================================================================
    # BRAND X — 10 posts (technical build-log beats)
    # ====================================================================
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Our trading ensemble runs 9 bots with snake names — Viper, Cobra, "
            "Mamba, Anaconda, Hydra, Taipan, Indian King Cobra, Terciopelo.\n\n"
            "Names aren't decoration. When Mamba's weight drops and Hydra's rises, "
            "the message is concrete.\n\n"
            f"{_GH_LINK}"
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Oracle coordination in our trading stack: individual bots don't know "
            "the others exist.\n\n"
            "The Oracle reads correlation between their positions and reduces "
            "total exposure where they overlap.\n\n"
            "Two great strategies at 0.9 correlation add less than one uncorrelated one."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Added a news-aware gate on Terciopelo this week.\n\n"
            "Before a relative-value signal fires, a small classifier checks for "
            "active earnings windows and macro calendar events. Entry is skipped "
            "inside those windows.\n\n"
            "Shadow-mode for two weeks before we size it up."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Priya is running in production.\n\n"
            "Bilingual Hindi/English voice agent for Cash on Delivery order "
            "confirmation in India. Sarvam AI + LiveKit + GPT-4o-mini.\n\n"
            "We picked Sarvam over Western TTS for native Hindi phoneme quality."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Trading Core is the shared library under all our bots. Position "
            "sizing, risk limits, broker abstraction.\n\n"
            "Extracted after two bots had position-sizing implementations that "
            "differed by a rounding error. In live trading, rounding errors "
            "become real losses."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Cricket engine needed separate calibration for IPL vs PSL.\n\n"
            "IPL pitches produce more pace-bowling dominance; PSL is more "
            "spin-friendly. One global ball-by-ball model underperformed on both.\n\n"
            "Per-competition tuning now."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Autonomous action layer on AI Marketing Stack:\n\n"
            "— Scans Meta adsets every 4h\n"
            "— Proposes pause / scale in Telegram with Approve / Reject\n"
            "— Approved actions execute in 5 minutes via glitch-ads-mcp\n"
            "— 72h rollback snapshot on every change\n\n"
            f"{_GH_LINK}/ai_marketing_stack-ai-ads-agent"
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Sessions-delta attribution is our workaround for brands locked out "
            "of the Amazon Attribution API (India-only brand registry).\n\n"
            "Infer conversions by matching session-traffic deltas against ad "
            "spend windows. Rough, but beats the Meta dashboard's blind spot."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Built a LinkedIn PDF carousel generator.\n\n"
            "LLM writes slides → fal.ai FLUX renders backgrounds → Pillow "
            "composites brand text → img2pdf compiles.\n\n"
            "~10 seconds, ~$0.02 per 7-slide deck. Carousels land 6x more "
            "engagement than text on LinkedIn."
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_x",
        "body": (
            "Comment engagement on LinkedIn is weighted ~15× more than likes.\n\n"
            "Wired up a sweep: our agent pulls comments on recent posts, drafts "
            "a reply in brand voice, sends a Telegram preview with approve / "
            "skip. Reply goes out after tap.\n\n"
            "Same HITL shape as our Oracle."
        ),
    },

    # ====================================================================
    # FOUNDER X — 5 posts (feeling / learning)
    # ====================================================================
    {
        "brand_id": "glitch_founder", "platform": "upload_post_x",
        "body": (
            "I keep landing on the same architecture: agent does the watching "
            "and reasoning, I tap approve in Telegram.\n\n"
            "Tried full autonomy on money decisions once. Too risky when "
            "attribution itself is noisy."
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_x",
        "body": (
            "The moment the trading Oracle coordinated its first risk reduction "
            "between Viper and Cobra on a live account, I realized the hard part "
            "had never been the individual models.\n\n"
            "It's always the coordination layer."
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_x",
        "body": (
            "48 hours on a problem that turned out to be one flag in the Oracle "
            "config.\n\n"
            "Those 48 hours taught me more about the system than the previous "
            "two weeks."
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_x",
        "body": (
            "Spent a day comparing the real Meta→Amazon attribution numbers to "
            "what the Meta dashboard was showing us.\n\n"
            "Watching the invisible show up in a sheet has a specific feeling."
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_x",
        "body": (
            "I was convinced ensemble models were an elegance choice.\n\n"
            "Three live applications later — trading, sports pricing, ad ops — "
            "they're just the only way I know to ship AI that doesn't collapse "
            "outside its training distribution."
        ),
    },

    # ====================================================================
    # BRAND LinkedIn — 5 longer technical posts
    # ====================================================================
    {
        "brand_id": "glitch_executor", "platform": "upload_post_linkedin",
        "body": (
            "Our trading ensemble runs nine bots with snake names. It looks "
            "whimsical. It isn't.\n\n"
            "Viper runs momentum. Anaconda runs mean reversion. Hydra adapts "
            "sizing on drawdowns. Indian King Cobra handles timeframe-aware "
            "momentum with ML gating. Terciopelo runs equities relative-value. "
            "Mamba, Taipan, Cobra, and Ouroboros round out the set.\n\n"
            "Individual bots don't know the others exist. An Oracle layer sits "
            "above them, reads correlation between their positions in near-real-time, "
            "and reduces total exposure when they overlap. That's the entire "
            "trick. Two great strategies at 0.9 correlation add less than one "
            "uncorrelated strategy.\n\n"
            "Broker-portable by design — an abstraction layer handles MT5, "
            "cTrader, and Interactive Brokers without strategy code changes.\n\n"
            "We learned the snake naming is not a gimmick the first time a "
            "3am debug session asked us which bot just cut exposure. "
            "\"Anaconda reduced on rising correlation with Hydra\" parses faster "
            "than \"strategy_4 reduced on rising correlation with strategy_7.\"\n\n"
            f"{_GH_LINK}/glitch-ouroboros-snake-strategy\n\n"
            "#algotrading #quantfinance #buildinpublic"
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_linkedin",
        "body": (
            "AI Marketing Stack's autonomous action layer went live this week.\n\n"
            "The agent scans Meta adsets on a 4-hour cadence. When a delta shift "
            "crosses a threshold — a performer worth scaling, a drifter worth "
            "pausing — it builds an Action Proposal and posts it to a Telegram "
            "group with Approve / Reject buttons.\n\n"
            "Approved proposals execute within 5 minutes via glitch-ads-mcp. "
            "Every action carries a prior-state snapshot and a 72-hour rollback "
            "window. No change touches a live account without a human tap and "
            "an undo path.\n\n"
            "The proposal isn't just \"raise budget\" — it carries the 14-day "
            "spend, the ROAS over that window, click count, expected incremental "
            "spend and revenue, and the auto-revert condition. Operators approve "
            "faster when they can sanity-check the reasoning.\n\n"
            "The pattern that keeps showing up: full autonomy on money decisions "
            "is too risky when attribution is noisy. Asking for approval on "
            "everything is useless — the operator becomes the bottleneck. The "
            "middle path is an agent that does the hard work of watching and "
            "reasoning, and asks the human for exactly one thing: the go-ahead.\n\n"
            f"{_GH_LINK}/ai_marketing_stack-ai-ads-agent\n\n"
            "#metaads #adops #buildinpublic"
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_linkedin",
        "body": (
            "Priya is a bilingual voice AI agent that calls Indian e-commerce "
            "customers to confirm Cash on Delivery orders before dispatch. "
            "She's running in production.\n\n"
            "The hard part wasn't the agent logic. It was the language. Indian "
            "customers hang up on English-only IVR. We needed real Hindi "
            "phoneme quality, not TTS with an accent. That ruled out most "
            "Western providers.\n\n"
            "Stack: Sarvam AI Bulbul v3 for the Hindi voice, LiveKit for the "
            "real-time WebRTC layer, GPT-4o-mini for the conversation loop. "
            "Multi-tenant from day one — per-shop prompts, voices, and languages "
            "are driven by Shopify metafields, so one deployment serves many "
            "stores without code changes.\n\n"
            "The failure mode we didn't anticipate: Indian PSTN drops calls "
            "unpredictably at certain times of day. We built retry logic and a "
            "fallback SMS path before we understood why we needed them.\n\n"
            f"{_GH_LINK}/glitch-cod-confirm\n\n"
            "#voiceai #ecommerce #india #buildinpublic"
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_linkedin",
        "body": (
            "Most e-commerce founders trust the Meta dashboard. The dashboard "
            "is structurally blind to Amazon orders — Amazon PDPs don't carry "
            "the Meta pixel, so any click-to-Amazon conversion disappears.\n\n"
            "For one client on our AI Marketing Stack stack, the Meta dashboard read "
            "1.22× ROAS. Our sessions-delta attribution model, running over the "
            "same 14-day window, read 1.57×. The gap is every Amazon order "
            "Meta couldn't see.\n\n"
            "The real fix is an Amazon LWA OAuth pipeline piping Amazon Seller "
            "Central orders into Meta CAPI server-side. We've built that end "
            "to end — token store, FastAPI callback, Cloudflare Pages Function "
            "— and we're waiting on Amazon Partner Network approval to enable "
            "live writes.\n\n"
            "Until that lands, sessions-delta is a reasonable stopgap. The "
            "unlock is that it works for India-only brand registries, which "
            "the Amazon Attribution API refuses to talk to.\n\n"
            f"{_GH_LINK}/ai_marketing_stack-ai-ads-agent\n\n"
            "#metaads #amazon #attribution #ecommerce"
        ),
    },
    {
        "brand_id": "glitch_executor", "platform": "upload_post_linkedin",
        "body": (
            "The cricket engine was the most humbling model we've built this year.\n\n"
            "Cricket is the second-largest sports betting market in the world and "
            "mostly underserved by Western quant shops. Ball-by-ball data is "
            "rich, but a T20 match can swing entirely on one over. Standard "
            "win-probability models that don't account for match-state momentum "
            "are too slow to be useful in-play.\n\n"
            "We built scenario modelling instead: given this batter, this bowler, "
            "this field setting, and the current required run rate, what's the "
            "probability distribution of outcomes in the next six balls? That "
            "projection feeds into pricing.\n\n"
            "We paper-simulated a full IPL season before going live. The paper "
            "simulation framework is built into the engine — it replays historical "
            "matches through the live code path. Found pricing bugs that would "
            "have cost real money.\n\n"
            "The surprise: IPL and PSL needed separate calibration. Pitch "
            "conditions differ enough that one global model underperformed on "
            "both. Per-competition tuning now.\n\n"
            f"{_GH_LINK}/glitch-cricket-engine\n\n"
            "#sportsanalytics #cricket #ml #buildinpublic"
        ),
    },

    # ====================================================================
    # FOUNDER LinkedIn — 5 posts (learning / feeling)
    # ====================================================================
    {
        "brand_id": "glitch_founder", "platform": "upload_post_linkedin",
        "body": (
            "I had a specific kind of moment yesterday.\n\n"
            "My ads agent proposed a budget change on a live client account. "
            "Client tapped Approve in Telegram. Meta API updated 4 minutes later. "
            "The whole loop — measurement, proposal, approval, execution — "
            "closed without me touching it.\n\n"
            "The part I wasn't ready for is how small it felt. I'd been picturing "
            "the moment as big. It was boring. The agent did what it was supposed "
            "to do, the client did what they were supposed to do, Meta did what "
            "it was supposed to do.\n\n"
            "Boring is the goal, I think. It means the system is working.\n\n"
            "The things that stayed interesting this week were the ones that "
            "broke. A /roas command that reported three times the actual number "
            "because I was summing duplicate Meta action_type aliases. A Telegram "
            "preview that hit the bot token before the admin had opened a chat. "
            "A Gemini rate-limit that revealed I didn't have retry logic.\n\n"
            "Each was a one-line fix. The lessons weren't about the code.\n\n"
            f"{_GH_LINK}/ai_marketing_stack-ai-ads-agent\n\n"
            "#buildinpublic #founder #metaads"
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_linkedin",
        "body": (
            "The thing I keep getting wrong about AI autonomy:\n\n"
            "I think I want the agent to do more. Then I ship a version that "
            "does more, and immediately start wanting the approval gate back.\n\n"
            "It happened with the trading Oracle. It happened with Priya's "
            "call logic. It happened with the ads agent's action layer. "
            "Every time I take the human out of the loop on a money decision, "
            "I put them back in within days.\n\n"
            "Full autonomy is too risky when attribution is noisy — which it "
            "always is. Asking for approval on everything is useless — the "
            "operator becomes the bottleneck. The middle path is the agent "
            "that watches, reasons, builds a proposal with evidence attached, "
            "and asks the human for exactly one thing: the go-ahead.\n\n"
            "I find myself designing every new system around this shape now, "
            "almost by reflex. Not because I'm opinionated about HITL. Because "
            "I keep trying the alternatives and they keep not working.\n\n"
            f"{_GH_LINK}\n\n"
            "#buildinpublic #ai #founder"
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_linkedin",
        "body": (
            "Something I didn't expect about building across three product "
            "lines at once:\n\n"
            "The pattern generalizes harder than the stack does.\n\n"
            "We run trading ensembles, sports intelligence engines, and "
            "e-commerce AI. Different code, different data, different customers. "
            "On paper, three unrelated bets.\n\n"
            "In practice, I spend almost all of my design time on the same "
            "three questions every time:\n"
            "— What's the coordination layer sitting above the individual models?\n"
            "— Where does the human approve, and what do they approve with?\n"
            "— What's the rollback story if we're wrong?\n\n"
            "The answers differ in detail. The shape of the answers doesn't.\n\n"
            "I started this project thinking I was picking markets. I "
            "realized this month I'm really picking problems that share a "
            "solution shape, and shipping them into whichever market noticed.\n\n"
            f"{_GH_LINK}\n\n"
            "#buildinpublic #ai #founder"
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_linkedin",
        "body": (
            "A bug I'm not proud of:\n\n"
            "My /roas command reported 3.67× ROAS for a client last week. "
            "Their Meta dashboard said 1.22×. They caught the discrepancy "
            "by eyeballing.\n\n"
            "Turned out I was summing five duplicate Meta action_type aliases "
            "— purchase, omni_purchase, pixel_purchase, and two more. Each one "
            "roughly represented the same conversion, just from a different "
            "attribution path. Meta's canonical is omni_purchase.\n\n"
            "Fixed it in 20 minutes. The post-mortem took longer than the fix.\n\n"
            "Two things I'm carrying out of it. First, a dashboard number and "
            "my number have to match to the decimal during testing, and if they "
            "don't I have to figure out why before I do anything else. Second, "
            "the client catching it in public was better than me catching it "
            "in private — it forced an honest fix and an honest conversation "
            "about which of us trusts which number.\n\n"
            "Public mistakes build faster than private ones. That one's now load-bearing.\n\n"
            f"{_GH_LINK}/ai_marketing_stack-ai-ads-agent\n\n"
            "#buildinpublic #founder"
        ),
    },
    {
        "brand_id": "glitch_founder", "platform": "upload_post_linkedin",
        "body": (
            "Building at this pace has a specific texture I didn't expect.\n\n"
            "It's not sustained intensity. It's a weird sawtooth. Long days of "
            "\"I can't see the edges of the problem\" followed by 45 minutes "
            "where the whole thing clicks and three unrelated systems fall into "
            "place at once.\n\n"
            "The trading Oracle clicked like this in February. The ads action "
            "layer clicked like this last week. Both times, I'd been circling "
            "the problem for days and would have sworn I was stuck.\n\n"
            "The thing I've stopped doing: trying to force the click. I've "
            "started treating the circling as information. It's not failure — "
            "it's the cost of noticing all the places the obvious solution "
            "doesn't hold.\n\n"
            "I used to feel embarrassed about how messy a week could look. "
            "Now I look at the messy weeks and assume a click is near. Whether "
            "it actually is, I don't fully know. But treating it that way beats "
            "the alternative.\n\n"
            f"{_GH_LINK}\n\n"
            "#buildinpublic #founder"
        ),
    },
]


# ---------------------------------------------------------------------------
# Sheet create + seed
# ---------------------------------------------------------------------------

def _create_spreadsheet_sync(title: str) -> tuple[str, str]:
    """Create a new spreadsheet owned by the service account. Returns
    (spreadsheet_id, url)."""
    svc = _service()
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": settings().glitch_posts_worksheet or "queue"}}],
    }
    resp = svc.spreadsheets().create(
        body=body,
        fields="spreadsheetId,spreadsheetUrl",
    ).execute()
    return resp["spreadsheetId"], resp["spreadsheetUrl"]


async def _create_and_share(title: str, share_with_email: str | None) -> tuple[str, str]:
    sheet_id, url = await asyncio.to_thread(_create_spreadsheet_sync, title)
    if share_with_email:
        try:
            await asyncio.to_thread(_share_sync, sheet_id, share_with_email)
        except Exception as exc:
            print(f"warning: share failed — {exc}", file=sys.stderr)
    return sheet_id, url


def _share_sync(sheet_id: str, email: str) -> None:
    """Give edit access to a user via the Drive API."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_path = settings().google_drive_sa_json
    creds = Credentials.from_service_account_file(
        sa_path,
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    drive.permissions().create(
        fileId=sheet_id,
        body={"type": "user", "role": "writer", "emailAddress": email},
        fields="id",
        sendNotificationEmail=False,
    ).execute()


async def seed_rows(sheet_id: str, worksheet: str, rows: list[dict[str, str]]) -> int:
    """Write header + append each row with a uuid id and status=queued."""
    await ensure_header(sheet_id=sheet_id, worksheet=worksheet, columns=SHEET_COLUMNS)

    written = 0
    for r in rows:
        full = {col: "" for col in SHEET_COLUMNS}
        full["id"] = str(uuid.uuid4())
        full["brand_id"] = r["brand_id"]
        full["platform"] = r["platform"]
        full["body"] = r["body"].strip()
        full["status"] = "queued"
        await append_row(sheet_id=sheet_id, worksheet=worksheet, columns=SHEET_COLUMNS, row=full)
        written += 1
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--share-with",
        help="Email to share edit access with (recommended).",
    )
    parser.add_argument(
        "--title",
        default="Glitch posts queue",
        help="Spreadsheet title (default: 'Glitch posts queue').",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Append seed rows even if GLITCH_POSTS_SHEET_ID is already set. Never overwrites existing rows.",
    )
    parser.add_argument(
        "--sheet-id",
        default=None,
        help="Skip creation; seed into an existing sheet by ID.",
    )
    args = parser.parse_args()

    s = settings()

    if args.sheet_id:
        sheet_id = args.sheet_id
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        print(f"Using existing sheet: {url}")
    elif s.glitch_posts_sheet_id and not args.force:
        print(
            f"GLITCH_POSTS_SHEET_ID already set ({s.glitch_posts_sheet_id}). "
            "Refusing to create a new sheet. Pass --force to append seed rows "
            "to the existing sheet, or --sheet-id <id> to seed into a "
            "different sheet.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print(f"Creating spreadsheet: {args.title!r} …")
        sheet_id, url = await _create_and_share(args.title, args.share_with)
        print(f"✓ Created: {url}")
        print(f"✓ Sheet ID: {sheet_id}")
        print(
            "\nAdd this to your .env:\n"
            f"  GLITCH_POSTS_SHEET_ID={sheet_id}\n"
        )

    print(f"\nSeeding {len(SEED_POSTS)} drafted posts …")
    written = await seed_rows(
        sheet_id, s.glitch_posts_worksheet or "queue", SEED_POSTS,
    )
    print(f"✓ Wrote {written} rows (status=queued).")
    print(
        "\nNext steps:\n"
        "  1. Add GLITCH_POSTS_SHEET_ID to .env (if you haven't).\n"
        "  2. Restart ai-social-media-agent.\n"
        "  3. Scheduler tick will pick up one due row every ~5 min, respecting "
        "the 4h per-(brand,platform) interval and 2/day cap set in settings.\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
