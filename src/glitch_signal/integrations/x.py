"""Direct X (Twitter) API client.

Replaces Upload-Post for X-specific surfaces Upload-Post doesn't expose:
  - Native reply (`POST /2/tweets` with `reply.in_reply_to_tweet_id`)
  - Quote-tweet (`POST /2/tweets` with `quote_tweet_id`)
  - Mentions read (`GET /2/users/:id/mentions`)

Auth comes from the platform_auth table via oauth.refresh.get_with_auto_refresh
so concurrent workers can't race each other into invalidated refresh tokens.
The X-specific refresh callback hits /2/oauth2/token with HTTP Basic auth
(client_id:client_secret) and returns a RefreshedTokens row.

Bot tokens (DM read/write, etc.) are out of scope for this module — we
add them when needed.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from glitch_signal.oauth.refresh import RefreshedTokens, get_with_auto_refresh
from glitch_signal.oauth.storage import PlainAuth

log = structlog.get_logger(__name__)

API = "https://api.twitter.com"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class XError(RuntimeError):
    """Non-recoverable X API error."""


class XRetryableError(XError):
    """Retryable error (5xx / 429 / network) so tenacity catches it."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class TweetResult:
    tweet_id: str
    tweet_url: str


@dataclass
class Mention:
    """Subset of /2/users/:id/mentions response we care about."""
    id: str
    text: str
    author_id: str
    author_username: str | None
    in_reply_to_user_id: str | None
    conversation_id: str | None
    created_at: str | None
    referenced_tweet_id: str | None  # the original tweet they replied to, if any


# ---------------------------------------------------------------------------
# Client (one per brand_id, since auth is per-account)
# ---------------------------------------------------------------------------

class XClient:
    """Per-brand X client. Auto-refreshes the OAuth2 access token via
    platform_auth before every request.
    """

    def __init__(self, brand_id: str) -> None:
        self.brand_id = brand_id
        self._client_id = os.environ.get(f"X_{_brand_env(brand_id)}_CLIENT_ID", "")
        self._client_secret = os.environ.get(f"X_{_brand_env(brand_id)}_CLIENT_SECRET", "")
        if not self._client_id or not self._client_secret:
            raise XError(
                f"X_{_brand_env(brand_id)}_CLIENT_ID / _SECRET not set in env"
            )

    async def _auth(self) -> PlainAuth:
        """Get a guaranteed-fresh PlainAuth row for this brand on platform=x."""
        return await get_with_auto_refresh(
            brand_id=self.brand_id,
            platform="x",
            refresh_callback=self._refresh_callback,
            safety_margin_s=120,  # refresh if <2 min left
        )

    async def _refresh_callback(self, refresh_token: str) -> RefreshedTokens:
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{API}/2/oauth2/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                },
            )
        if r.status_code != 200:
            raise XError(f"refresh failed {r.status_code}: {r.text[:300]}")
        d = r.json()
        scopes_field = d.get("scope", "")
        scopes = scopes_field.split() if isinstance(scopes_field, str) else None
        return RefreshedTokens(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token"),
            expires_in_s=int(d.get("expires_in", 7200)),
            scopes=scopes,
            raw_response=d,
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, XRetryableError)),
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        auth = await self._auth()
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.request(
                method,
                f"{API}{path}",
                headers={"Authorization": f"Bearer {auth.access_token}"},
                params=params,
                json=json,
            )
        if resp.status_code in (429, 500, 502, 503, 504):
            raise XRetryableError(
                f"{method} {path} -> {resp.status_code} (retryable): "
                f"{resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise XError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp

    # -- Tweets --------------------------------------------------------------

    async def post_tweet(
        self,
        text: str,
        *,
        in_reply_to_tweet_id: str | None = None,
        quote_tweet_id: str | None = None,
    ) -> TweetResult:
        """Post a tweet. Pass `in_reply_to_tweet_id` for a reply, or
        `quote_tweet_id` for a quote-tweet. Both is invalid (X rejects).
        """
        body: dict = {"text": text}
        if in_reply_to_tweet_id:
            body["reply"] = {"in_reply_to_tweet_id": str(in_reply_to_tweet_id)}
        if quote_tweet_id:
            body["quote_tweet_id"] = str(quote_tweet_id)
        resp = await self._request("POST", "/2/tweets", json=body)
        data = resp.json().get("data") or {}
        tweet_id = data.get("id")
        if not tweet_id:
            raise XError(f"post_tweet: no id in response {resp.text[:300]}")
        # We need the username to build the URL. _auth already gave us the
        # user_id but not the username; fall back to a generic URL pattern
        # that always redirects correctly on x.com.
        url = f"https://x.com/i/web/status/{tweet_id}"
        return TweetResult(tweet_id=str(tweet_id), tweet_url=url)

    # -- Mentions ------------------------------------------------------------

    async def get_mentions(
        self,
        user_id: str,
        *,
        since_id: str | None = None,
        max_results: int = 50,
    ) -> list[Mention]:
        """Fetch mentions of `user_id`, newest first. since_id makes it
        an incremental sweep — pass the highest id we've seen previously.
        """
        params: dict = {
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": "author_id,conversation_id,created_at,referenced_tweets,in_reply_to_user_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        if since_id:
            params["since_id"] = since_id
        resp = await self._request(
            "GET", f"/2/users/{user_id}/mentions", params=params,
        )
        payload = resp.json()
        data = payload.get("data") or []
        users_index: dict[str, dict] = {
            u["id"]: u for u in (payload.get("includes", {}).get("users") or [])
        }

        out: list[Mention] = []
        for t in data:
            ref_tweet_id = None
            for ref in t.get("referenced_tweets") or []:
                if ref.get("type") in ("replied_to", "quoted"):
                    ref_tweet_id = ref.get("id")
                    break
            author_id = t.get("author_id", "")
            author_username = users_index.get(author_id, {}).get("username")
            out.append(Mention(
                id=str(t["id"]),
                text=t.get("text", ""),
                author_id=str(author_id),
                author_username=author_username,
                in_reply_to_user_id=t.get("in_reply_to_user_id"),
                conversation_id=t.get("conversation_id"),
                created_at=t.get("created_at"),
                referenced_tweet_id=ref_tweet_id,
            ))
        return out

    # -- Convenience --------------------------------------------------------

    async def get_self(self) -> dict:
        """GET /2/users/me — returns the account this token represents."""
        resp = await self._request("GET", "/2/users/me")
        return resp.json().get("data") or {}

    async def get_reply_settings(self, tweet_id: str) -> str:
        """Return the author's reply_settings on a target tweet.

        Possible values per X docs:
          "everyone"        — anyone can reply
          "mentionedUsers"  — only users mentioned in the tweet can reply
          "following"       — only users the author follows can reply
          "subscribers"     — only paying subscribers can reply
          "verified"        — only verified accounts can reply

        Returns an empty string on lookup failure; caller should treat
        that as "unknown — proceed and let the post call fail" rather
        than blocking optimistically.
        """
        try:
            resp = await self._request(
                "GET", f"/2/tweets/{tweet_id}",
                params={"tweet.fields": "reply_settings"},
            )
        except Exception as exc:
            log.info("x.reply_settings_lookup_failed", tweet_id=tweet_id, error=str(exc)[:200])
            return ""
        data = resp.json().get("data") or {}
        return str(data.get("reply_settings") or "").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _brand_env(brand_id: str) -> str:
    """Map brand_id → ENV-prefix segment (X_BRAND_… / X_FOUNDER_…)."""
    if brand_id == "glitch_executor":
        return "BRAND"
    if brand_id == "glitch_founder":
        return "FOUNDER"
    raise XError(f"no X env mapping for brand_id={brand_id!r}")


def client_for(brand_id: str) -> XClient | None:
    """Build a client; return None if env credentials aren't set."""
    try:
        return XClient(brand_id)
    except XError:
        return None
