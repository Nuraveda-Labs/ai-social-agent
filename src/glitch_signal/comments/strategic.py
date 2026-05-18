"""Strategic reply — draft a reply to someone else's post on X or LinkedIn.

Covers the 70/30 growth pattern: most follower growth in the
build-in-public space comes from value-adding replies to larger accounts'
posts, not from posting in a vacuum. This module lets the operator drop
a URL into Telegram and have the agent draft a voice-matched reply.

Flow:
    /reply <url>                              (default account = glitch_founder)
    /reply <url> brand                        (use brand voice)
    /reply <url> founder                      (use founder voice)
          ↓
    1. Parse URL → detect platform (x|linkedin)
    2. Fetch the target post's text:
         - X: Twitter oEmbed → scrape a no-auth public view
         - LinkedIn: fetch OG meta description (works for many public posts)
       Fall back to "operator supplies the text" if fetch fails.
    3. LLM drafts a reply in the chosen brand voice.
    4. Save StrategicReply row, status=pending_approval.
    5. Telegram preview with Approve / Skip.
    6. Approve →
         - X:        post via Upload-Post upload_text + quote_tweet_id (quote-
                     reply, not threaded reply — but still engages with the
                     post and surfaces in our own feed).
         - LinkedIn: we cannot programmatically comment on arbitrary LinkedIn
                     posts via any audited API. Status flips to "copied" and
                     we hand the drafted reply text back to the operator in
                     Telegram for manual paste.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import uuid
from datetime import UTC, datetime

import httpx
import litellm
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.agent.llm import pick
from glitch_signal.agent.nodes.text_writer import _forbidden_hits
from glitch_signal.config import brand_config, settings
from glitch_signal.db.models import StrategicReply
from glitch_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

# Target post text >N chars gets truncated in the LLM context. We're drafting
# a reply, not a summary — the first N chars are plenty.
TARGET_TEXT_MAX = 1500


async def queue_strategic_reply(
    *,
    target_url: str,
    brand_id: str,
    requested_by: str | None = None,
) -> tuple[str | None, str]:
    """Fetch the target post, draft a reply, save as pending_approval.

    Returns (strategic_reply_id | None, status_message).
    """
    platform, post_id = _classify_url(target_url)

    # Reply-restriction pre-check (X only). If the author has limited
    # who can reply, we'd rather know now and skip the LLM draft entirely
    # than burn tokens on a reply that can't physically post. There's no
    # equivalent control on LinkedIn, so we only probe X.
    if platform == "x" and post_id:
        try:
            from glitch_signal.integrations.x import XClient
            blocked = await XClient(brand_id).get_reply_settings(post_id)
        except Exception:
            blocked = ""
        if blocked and blocked != "everyone":
            return None, (
                f"Author has restricted replies (reply_settings={blocked!r}). "
                "X won't accept a threaded reply from us. Skipping draft. "
                "If you want to engage anyway, post your own tweet that "
                "links to theirs."
            )

    # Fetch target post text (best-effort)
    try:
        target_text, author_handle = await _fetch_target_post(target_url, platform)
    except Exception as exc:
        log.warning("strategic.fetch_failed", url=target_url, error=str(exc)[:200])
        target_text, author_handle = "", None

    if not target_text:
        return None, (
            "Couldn't auto-fetch the post text. Retry with the text pasted:\n"
            "/reply_with_text <url> :: <pasted post text>"
        )

    try:
        drafted = await _draft_strategic_reply(
            brand_id=brand_id,
            platform=platform,
            target_text=target_text,
            author_handle=author_handle,
        )
    except Exception as exc:
        log.warning("strategic.draft_failed", error=str(exc)[:200])
        return None, f"LLM draft failed: {exc}"

    now = datetime.now(UTC).replace(tzinfo=None)
    sr_id = str(uuid.uuid4())
    factory = _session_factory()
    async with factory() as session:
        row = StrategicReply(
            id=sr_id,
            brand_id=brand_id,
            target_platform=platform,
            target_post_url=target_url,
            target_post_id=post_id,
            target_author_handle=author_handle,
            target_post_text=target_text[:TARGET_TEXT_MAX],
            drafted_reply=drafted,
            status="pending_approval",
            requested_by_telegram_id=requested_by,
            created_at=now,
        )
        session.add(row)
        await session.commit()

    return sr_id, drafted


async def queue_from_text(
    *,
    target_url: str,
    target_text: str,
    brand_id: str,
    requested_by: str | None = None,
) -> tuple[str | None, str]:
    """Operator supplied the target post text inline (for when fetch failed)."""
    platform, post_id = _classify_url(target_url)
    try:
        drafted = await _draft_strategic_reply(
            brand_id=brand_id,
            platform=platform,
            target_text=target_text,
            author_handle=None,
        )
    except Exception as exc:
        return None, f"LLM draft failed: {exc}"

    now = datetime.now(UTC).replace(tzinfo=None)
    sr_id = str(uuid.uuid4())
    factory = _session_factory()
    async with factory() as session:
        row = StrategicReply(
            id=sr_id,
            brand_id=brand_id,
            target_platform=platform,
            target_post_url=target_url,
            target_post_id=post_id,
            target_post_text=target_text[:TARGET_TEXT_MAX],
            drafted_reply=drafted,
            status="pending_approval",
            requested_by_telegram_id=requested_by,
            created_at=now,
        )
        session.add(row)
        await session.commit()

    return sr_id, drafted


# ---------------------------------------------------------------------------
# URL classification + fetch
# ---------------------------------------------------------------------------

_X_HOSTS = ("x.com", "twitter.com", "mobile.twitter.com", "www.x.com")
_LI_HOSTS = ("linkedin.com", "www.linkedin.com", "lnkd.in")


def _classify_url(url: str) -> tuple[str, str | None]:
    """Return (platform, post_id). Platform is x|linkedin|unknown.

    For LinkedIn we extract the URN if the URL carries one (which the
    /feed/update/ form does). Examples:
      https://www.linkedin.com/feed/update/urn:li:activity:7445...   -> activity:7445...
      https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7445   -> activity:7445...
      https://www.linkedin.com/posts/...activity-7445...-abcd          -> activity:7445...

    Returns the full urn string (e.g. "urn:li:activity:7445...") so the
    caller can hand it directly to LinkedIn API methods.
    """
    url = url.strip().rstrip("/")
    low = url.lower()

    for host in _X_HOSTS:
        if f"//{host}/" in low:
            # https://x.com/<user>/status/<id>
            m = re.search(r"/status/(\d+)", url)
            return "x", (m.group(1) if m else None)

    for host in _LI_HOSTS:
        if f"//{host}/" in low:
            # /feed/update/urn:li:activity:NNN  (or url-encoded variant)
            decoded = url.replace("%3A", ":").replace("%3a", ":")
            m = re.search(
                r"urn:li:(?:activity|share|ugcPost|comment):[A-Za-z0-9._-]+",
                decoded,
            )
            if m:
                return "linkedin", m.group(0)
            # /posts/<slug>-activity-NNN-... — synthesize the URN.
            m2 = re.search(r"-activity-(\d+)-", url)
            if m2:
                return "linkedin", f"urn:li:activity:{m2.group(1)}"
            # /posts/<slug>-share-NNN-... — newer URL form using share id.
            # The numeric id resolves the same activity, so we synthesize
            # an activity URN; LinkedIn's socialActions endpoint accepts it.
            m3 = re.search(r"-share-(\d+)-", url)
            if m3:
                return "linkedin", f"urn:li:activity:{m3.group(1)}"
            return "linkedin", None

    return "unknown", None


async def _fetch_target_post(url: str, platform: str) -> tuple[str, str | None]:
    """Return (post_text, author_handle). Best-effort; may return ('', None)."""
    if platform == "x":
        return await _fetch_x_post(url)
    if platform == "linkedin":
        return await _fetch_linkedin_post(url)
    return "", None


async def _fetch_x_post(url: str) -> tuple[str, str | None]:
    """Use X's public oEmbed endpoint — no auth, returns post text as HTML blob."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                "https://publish.twitter.com/oembed",
                params={"url": url, "omit_script": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.info("strategic.x_oembed_failed", error=str(exc)[:200])
        return "", None

    html = data.get("html") or ""
    author = data.get("author_name") or data.get("author_url", "").rstrip("/").split("/")[-1]

    # Pull inner text from the blockquote. Reasonable regex strip — we're
    # not trying to render, just extract readable words for the LLM.
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[#a-z0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, author


async def _fetch_linkedin_post(url: str) -> tuple[str, str | None]:
    """Try to scrape OpenGraph meta tags. Not reliable on all LinkedIn URLs."""
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Glitch-Social-Agent/1.0)"
            },
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        log.info("strategic.linkedin_fetch_failed", error=str(exc)[:200])
        return "", None

    html = resp.text

    def _og(name: str) -> str:
        m = re.search(
            rf'<meta[^>]+property=["\']og:{name}["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        return m.group(1) if m else ""

    title = _og("title")
    description = _og("description")
    combined = (title + "\n\n" + description).strip()

    # OpenGraph meta tags double-encode HTML entities (&amp;#39; etc.) —
    # decode twice so the LLM sees clean text instead of "Don&amp;#39;t post".
    import html as _html
    combined = _html.unescape(_html.unescape(combined))

    # Try to pull the LinkedIn author from <meta property="og:author"> or
    # the author URL pattern in the page itself.
    author_handle = None
    m_author = re.search(
        r'<meta[^>]+(?:property|name)=["\']author["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m_author:
        author_handle = m_author.group(1).strip() or None
    if not author_handle:
        m_url = re.search(r'linkedin\.com/in/([A-Za-z0-9._-]+)', html)
        if m_url:
            author_handle = m_url.group(1)

    return combined, author_handle


# ---------------------------------------------------------------------------
# LLM draft
# ---------------------------------------------------------------------------

_STRATEGIC_SYSTEM = """You are writing a short reply to someone else's post on a professional social platform.

Your reply must do EXACTLY ONE of these:
  (a) Add a SPECIFIC concrete experience the commenter has lived — name a
      tool, a number, a decision, a moment. Generic agreement is rejected.
  (b) Push back on a SPECIFIC point the original post made, briefly.
  (c) Add ONE specific piece of information the post missed (a technique,
      a tradeoff, a counter-example).

When pulling from the identity file:
  - Pick ONE entry from the "What I'm shipping" section. Don't blend
    language from two different entries — that's what AI does, not what
    a human posting a reply does. If the cold-email story is the most
    relevant, write only about cold emails. Don't sprinkle in "reading
    the logs" from the voice-agent story.
  - Use language CLOSE to the wording in the identity file. Don't
    paraphrase a specific number into a vaguer one, and don't invent
    new specifics that aren't there.
  - If no identity entry is genuinely relevant to the original post,
    write a short statement of agreement or disagreement WITHOUT
    inventing a specific. Better to be terse than to fabricate.

The reply is REJECTED if it does any of:
  - Generic agreement: "great point", "love this", "totally agree", "fascinating".
  - Generic philosophical framing: "it's about blending X with Y",
    "creates content that resonates", "the journey of", "harnessing the power".
  - AI-consultant cadence: "It's fascinating how X can guide Y", "Blending
    A with B creates C that resonates beyond Z".
  - Marketing verbs: "leverages", "harnesses", "elevates", "transforms".
  - "In my experience," followed by something vague.
  - Any question. Statements only.
  - 1-3 sentences max. Never longer.
  - "Great post / totally agree" / "thanks for sharing" openers.
  - INVENTED METRICS. If the identity file uses vague language ("jumped",
    "better", "more replies", "a lot faster"), KEEP IT VAGUE. Don't
    quantify ("tripled", "2x", "50% better") unless the identity file
    contains that exact number. Hallucinating numbers gets the reply
    REJECTED — it's worse than generic agreement.
  - EXCLAMATION CLOSERS. "It's amazing what X can do!", "Such a
    powerful idea!", "Game-changer!" — every one of these is an AI
    tell. Strategic replies end on a beat, not a flourish. No "!".
  in the original post.
- No self-promotion. Never link to our own work unless the original post
  explicitly asked for examples.
- Never fabricate metrics. Never claim an outcome we haven't measured.
- If we genuinely disagree with the post, say so directly and briefly.

Output ONLY the reply text. No quotes, no JSON, no preamble."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _draft_strategic_reply(
    *,
    brand_id: str,
    platform: str,
    target_text: str,
    author_handle: str | None,
) -> str:
    cfg = brand_config(brand_id)
    voice_path = cfg.get("voice_prompt_path")
    voice_text = ""
    if voice_path:
        p = pathlib.Path(voice_path)
        if p.exists():
            voice_text = p.read_text()

    # Identity file — concrete specifics the LLM can pull from instead of
    # inventing generic agreement. Only present for brands that maintain one.
    identity_path = cfg.get("identity_prompt_path")
    identity_text = ""
    if identity_path:
        p = pathlib.Path(identity_path)
        if p.exists():
            identity_text = p.read_text()

    voice_role = (
        "VOICE IS TEJAS — first-person 'I', personal, lesson/feeling tone."
        if brand_id == "glitch_founder"
        else "VOICE IS GLITCH EXECUTOR — first-person plural 'we', technical, direct."
    )

    author_line = f"Posted by: @{author_handle}\n" if author_handle else ""

    identity_block = (
        f"---\n"
        f"WHO YOU ARE — pull specifics from here when drafting. NEVER\n"
        f"invent specifics that aren't in this file.\n\n"
        f"{identity_text}\n"
        if identity_text else ""
    )

    system = (
        f"{voice_text}\n\n"
        f"---\n"
        f"{voice_role}\n"
        f"{identity_block}"
        f"---\n"
        f"{_STRATEGIC_SYSTEM}"
    )
    user = (
        f"Platform: {platform}\n"
        f"{author_line}"
        f"Their post:\n{target_text[:TARGET_TEXT_MAX]}\n\n"
        f"Write the reply."
    )

    mc = pick("smart" if (settings().openai_api_key or settings().anthropic_api_key) else "cheap")
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=1024,
        **mc.kwargs,
    )
    body = (resp.choices[0].message.content or "").strip()
    body = _strip_quotes_and_framing(body)
    body = _scrub_em_dashes(body)

    # Anti-AI-tells regen — same filter the comment-reply drafter uses.
    # Catches "Curious if you've...", em-dashes, "the real lesson", etc.
    from glitch_signal.agent.nodes.text_writer import _x_specific_hits
    hits = _forbidden_hits(body)
    if platform in ("upload_post_x", "x", "linkedin"):
        hits = hits + _x_specific_hits(body)
    # Strategic replies must NEVER contain a question — extra catch.
    if "?" in body:
        hits.append("question (strategic replies make statements only)")
    # Exclamation marks read as AI on professional social. Tolerate at
    # most one if it's not the final character; reject at the end.
    if body.rstrip().endswith("!"):
        hits.append("exclamation closer (AI flourish)")
    if hits:
        log.info("strategic.forbidden_hits_regen", hits=hits, platform=platform)
        ban = (
            "Your last reply tripped these anti-AI / anti-bait rules: "
            + ", ".join(f'"{h}"' for h in hits)
            + ". Rewrite as a STATEMENT (no questions, no '?'), "
            "without any of the listed phrases. Same idea, different wording. "
            "Read it out loud — if it sounds like a human's casual reply, "
            "ship it. If it sounds like a polished essay or has a question, "
            "you're not done."
        )
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": body},
                {"role": "user", "content": ban},
            ],
            max_tokens=1024,
            **mc.kwargs,
        )
        body = _scrub_em_dashes(
            _strip_quotes_and_framing(
                (resp.choices[0].message.content or "").strip()
            )
        )

    hits = _forbidden_hits(body)
    if hits:
        log.info("strategic.forbidden_hits_regen", hits=hits)
        ban = (
            "Your last reply used banned phrases: "
            + ", ".join(f'"{h}"' for h in hits)
            + ". Rewrite without any of these phrases, and without any other "
            "hype adjectives or marketing verbs. Same content — just change wording."
        )
        resp = await litellm.acompletion(
            model=mc.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": body},
                {"role": "user", "content": ban},
            ],
            max_tokens=1024,
            **mc.kwargs,
        )
        body = _scrub_em_dashes(
            _strip_quotes_and_framing(
                (resp.choices[0].message.content or "").strip()
            )
        )

    return body


def _strip_quotes_and_framing(text: str) -> str:
    lines = text.split("\n")
    while lines and lines[0].lower().strip() in (
        "here's the reply:",
        "here is the reply:",
        "reply:",
    ):
        lines = lines[1:]
    out = "\n".join(lines).strip()
    if out.startswith('"') and out.endswith('"'):
        out = out[1:-1].strip()
    return out


def _scrub_em_dashes(text: str) -> str:
    """Replace em-dash (—) with comma + space. Em-dashes mid-sentence are
    the single highest-signal AI tell on social. We replace deterministically
    rather than trusting an LLM regen to drop it (it often doesn't)."""
    # Three patterns:
    #   "X — Y"  -> "X, Y"   (spaces on both sides)
    #   "X—Y"    -> "X, Y"   (no spaces)
    #   "X —Y"   -> "X, Y"   (mixed)
    out = text.replace(" — ", ", ")
    out = out.replace(" —", ",")
    out = out.replace("— ", " ")
    out = out.replace("—", ", ")
    return out


# ---------------------------------------------------------------------------
# Approve / veto — called from Telegram handlers
# ---------------------------------------------------------------------------

async def approve_strategic(sr_id: str) -> tuple[bool, str]:
    factory = _session_factory()
    async with factory() as session:
        row = await session.get(StrategicReply, sr_id)
        if not row:
            return False, f"strategic_reply {sr_id[:8]} not found"
        if row.status not in ("pending_approval",):
            return False, f"already {row.status}"
        if not row.drafted_reply:
            return False, "no drafted reply"

        cfg = brand_config(row.brand_id)
        platform_key = f"upload_post_{row.target_platform}"
        user = (cfg.get("platforms", {}).get(platform_key) or {}).get("user")

    api_key = settings().upload_post_api_key

    # LinkedIn: try to post the comment natively via /rest/socialActions.
    # On a 403 from the partnerApiSocialActions endpoint specifically, we
    # downgrade to "copied" so the operator can paste manually — that
    # endpoint is gated behind the Community Management API partnership,
    # SEPARATE from the Marketing Developer Platform tier we have today.
    # Once that approval lands, this same code path just starts working.
    if row.target_platform == "linkedin":
        if not row.target_post_id:
            return False, (
                "LinkedIn URL didn't carry a parseable URN. Paste the "
                "/feed/update/urn:li:activity:... form of the URL."
            )
        try:
            from glitch_signal.integrations.linkedin import (
                LinkedInError,
                author_urn_for,
                client_from_settings,
            )
            client = client_from_settings()
            if client is None:
                return False, "LINKEDIN_ACCESS_TOKEN unset"
            actor_urn = author_urn_for(row.brand_id)
            if not actor_urn:
                return False, f"no LinkedIn actor URN configured for {row.brand_id}"
            comment_urn = await client.create_comment(
                post_urn=row.target_post_id,
                actor_urn=actor_urn,
                text=row.drafted_reply,
            )
        except LinkedInError as exc:
            err = str(exc)
            # 403 from socialActions = Community Management API gate, not
            # a real failure. Downgrade to manual paste.
            if "partnerApiSocialActions" in err or (
                "403" in err and "/socialActions/" in err
            ):
                async with _session_factory()() as session:
                    copy_row = await session.get(StrategicReply, sr_id)
                    if copy_row:
                        copy_row.status = "copied"
                        copy_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                        session.add(copy_row)
                        await session.commit()
                return True, (
                    "LinkedIn Community Management API not approved yet — "
                    "/rest/socialActions/.../comments is gated separately "
                    "from MDP. Paste this manually:\n\n"
                    f"{row.drafted_reply}"
                )
            async with _session_factory()() as session:
                fail = await session.get(StrategicReply, sr_id)
                if fail:
                    fail.status = "failed"
                    fail.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    session.add(fail)
                    await session.commit()
            return False, f"LinkedIn comment post failed: {exc}"

        async with _session_factory()() as session:
            ok_row = await session.get(StrategicReply, sr_id)
            if ok_row:
                ok_row.status = "posted"
                ok_row.posted_platform_post_id = comment_urn
                ok_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(ok_row)
                await session.commit()
        return True, f"Comment posted on LinkedIn as {row.brand_id}: {comment_urn}"

    # X path: prefer the native /2/tweets endpoint (true threaded reply
    # via in_reply_to_tweet_id), fall back to Upload-Post quote_tweet_id
    # if our own token is unavailable. Native gives us the real reply
    # placement in the conversation; Upload-Post can only quote-tweet.
    if not row.target_post_id:
        return False, "missing target_post_id"

    posted_id: str | None = None
    via: str | None = None

    try:
        from glitch_signal.integrations.x import XClient
        x = XClient(row.brand_id)
        # Re-check reply settings at approval time in case the author
        # tightened them after we drafted. Cheap, single GET.
        if row.target_post_id:
            settings_now = await x.get_reply_settings(row.target_post_id)
            if settings_now and settings_now != "everyone":
                async with _session_factory()() as session:
                    blocked_row = await session.get(StrategicReply, sr_id)
                    if blocked_row:
                        blocked_row.status = "blocked_by_author"
                        blocked_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                        session.add(blocked_row)
                        await session.commit()
                return False, (
                    f"Author restricted replies (reply_settings={settings_now!r}). "
                    "Not falling back to a quote-tweet — that's a different "
                    "surface and you didn't ask for one."
                )
        result = await x.post_tweet(
            row.drafted_reply,
            in_reply_to_tweet_id=row.target_post_id,
        )
        posted_id = result.tweet_id
        via = "native-reply"
    except Exception as native_exc:
        # If the error is specifically X's reply-restriction signal, treat
        # it as blocked_by_author and refuse the quote-tweet fallback —
        # the operator asked for a reply, not a quote-tweet on our feed.
        err_text = str(native_exc).lower()
        is_reply_blocked = (
            "reply to this conversation is not allowed" in err_text
            or "not been mentioned or otherwise engaged" in err_text
        )
        if is_reply_blocked:
            async with _session_factory()() as session:
                blocked_row = await session.get(StrategicReply, sr_id)
                if blocked_row:
                    blocked_row.status = "blocked_by_author"
                    blocked_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    session.add(blocked_row)
                    await session.commit()
            return False, (
                "Author restricted replies on this tweet. Not falling back "
                "to a quote-tweet — that's a different surface."
            )

        log.info(
            "strategic.x_native_unavailable_falling_back_to_upload_post",
            sr_id=sr_id, error=str(native_exc)[:200],
        )
        if not user or not api_key:
            async with _session_factory()() as session:
                fail_row = await session.get(StrategicReply, sr_id)
                if fail_row:
                    fail_row.status = "failed"
                    fail_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    session.add(fail_row)
                    await session.commit()
            return False, f"native X failed and no Upload-Post fallback: {native_exc}"
        try:
            resp = await asyncio.to_thread(
                _post_x_quote_reply,
                api_key,
                user,
                row.drafted_reply,
                row.target_post_id,
            )
            via = "upload-post-quote"
            posted_id = resp.get("request_id") if isinstance(resp, dict) else None
        except Exception as exc:
            async with _session_factory()() as session:
                fail_row = await session.get(StrategicReply, sr_id)
                if fail_row:
                    fail_row.status = "failed"
                    fail_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    session.add(fail_row)
                    await session.commit()
            return False, f"X reply failed: native={native_exc!s}; fallback={exc!s}"

    async with _session_factory()() as session:
        ok_row = await session.get(StrategicReply, sr_id)
        if ok_row:
            ok_row.status = "posted"
            ok_row.posted_platform_post_id = posted_id
            ok_row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(ok_row)
            await session.commit()

    suffix = f" ({via})" if via else ""
    return True, f"Reply posted on X{suffix}."


async def veto_strategic(sr_id: str) -> tuple[bool, str]:
    factory = _session_factory()
    async with factory() as session:
        row = await session.get(StrategicReply, sr_id)
        if not row:
            return False, f"strategic_reply {sr_id[:8]} not found"
        row.status = "vetoed"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        session.add(row)
        await session.commit()
    return True, "Skipped."


def _post_x_quote_reply(api_key: str, user: str, text: str, quote_tweet_id: str) -> dict:
    import upload_post

    client = upload_post.UploadPostClient(api_key=api_key)
    return client.upload_text(
        title=text,
        user=user,
        platforms=["x"],
        quote_tweet_id=quote_tweet_id,
    )
