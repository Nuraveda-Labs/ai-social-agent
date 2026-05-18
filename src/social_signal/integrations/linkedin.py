"""Direct LinkedIn API client for the sheet-posting pipeline.

Replaces the Upload-Post path for upload_post_linkedin rows when
LINKEDIN_ACCESS_TOKEN is configured. Posts on both Tejas's profile
(w_member_social) and the this project company page
(w_organization_social) — same code path, the author URN switches.

Three primary flows:
  - text post:     post_text(author_urn, body)
  - image post:    post_image(author_urn, body, image_path)
  - document post: post_document(author_urn, body, pdf_path, title)

The document flow is the one we actually use for carousels:
    1. POST /rest/documents?action=initializeUpload
    2. PUT  <uploadUrl>  (raw PDF bytes)
    3. POST /rest/posts  with content.media.id = the document URN

All calls go through a shared httpx.AsyncClient with required LinkedIn
headers and tenacity retry on 5xx/429/network. Returns the synchronous
urn:li:share:... — no async reconcile needed.

Comment read/reply on company-page posts (r_organization_social /
w_organization_social) is a Phase 2 addition. Comment read/reply on the
founder's *personal* posts is gated behind r_member_social (separate
Community Management API for Members approval).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from social_signal.config import settings

log = structlog.get_logger(__name__)

API_HOST = "https://api.linkedin.com"
OAUTH_HOST = "https://www.linkedin.com"


class LinkedInError(RuntimeError):
    """Raised on a non-recoverable LinkedIn API error."""


class LinkedInRetryableError(LinkedInError):
    """Raised on a retryable error (5xx, 429, network) so tenacity catches it."""


@dataclass
class PostResult:
    """Outcome of a successful publish call."""
    post_urn: str            # urn:li:share:... or urn:li:ugcPost:...
    post_url: str            # https://www.linkedin.com/feed/update/<urn>/


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LinkedInClient:
    """Async LinkedIn Marketing Developer Platform client.

    All endpoints in /rest/* require both X-Restli-Protocol-Version: 2.0.0
    and a LinkedIn-Version header. We set both globally on the session.
    """

    def __init__(
        self,
        *,
        access_token: str,
        api_version: str = "202604",
        client_id: str = "",
        client_secret: str = "",
        refresh_token: str = "",
    ) -> None:
        if not access_token:
            raise LinkedInError("LinkedInClient requires an access_token")
        self._access_token = access_token
        self._api_version = api_version
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token

    # -- HTTP plumbing ------------------------------------------------------

    def _headers(self, *, with_version: bool = True) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        if with_version:
            h["LinkedIn-Version"] = self._api_version
        return h

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, LinkedInRetryableError)),
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        with_version: bool = True,
    ) -> httpx.Response:
        url = f"{API_HOST}{path}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(
                method,
                url,
                headers=self._headers(with_version=with_version),
                params=params,
                json=json,
            )
        # Classify retryable vs fatal so tenacity only retries the right ones.
        if resp.status_code in (429, 500, 502, 503, 504):
            raise LinkedInRetryableError(
                f"{method} {path} -> {resp.status_code} (retryable): {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise LinkedInError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp

    # -- OAuth refresh ------------------------------------------------------

    async def refresh(self) -> tuple[str, str | None]:
        """Exchange the stored refresh token for a new access token.

        Returns (new_access_token, new_refresh_token_or_None). Updates the
        in-memory access token. Caller is responsible for persisting both
        back to .env / platform_auth.
        """
        if not (self._refresh_token and self._client_id and self._client_secret):
            raise LinkedInError(
                "refresh() needs refresh_token + client_id + client_secret"
            )
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{OAUTH_HOST}/oauth/v2/accessToken",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        if r.status_code != 200:
            raise LinkedInError(f"refresh failed {r.status_code}: {r.text[:300]}")
        data = r.json()
        new_access = data["access_token"]
        new_refresh = data.get("refresh_token")  # may be None if not rotated
        self._access_token = new_access
        if new_refresh:
            self._refresh_token = new_refresh
        log.info(
            "linkedin.token_refreshed",
            expires_in=data.get("expires_in"),
            refresh_rotated=bool(new_refresh),
        )
        return new_access, new_refresh

    # -- Identity -----------------------------------------------------------

    async def get_userinfo(self) -> dict:
        """OpenID Connect /v2/userinfo. Returns sub (Person URN ID), name,
        email, email_verified."""
        # /v2/userinfo doesn't accept LinkedIn-Version header.
        resp = await self._request("GET", "/v2/userinfo", with_version=False)
        return resp.json()

    # -- Document upload (the carousel pipeline) ----------------------------

    async def register_document_upload(self, owner_urn: str) -> tuple[str, str]:
        """Initialize a document upload. Returns (upload_url, document_urn).

        owner_urn is whoever the document belongs to — must be the same URN
        that will author the resulting post:
          - urn:li:person:<id>          for founder posts
          - urn:li:organization:<id>    for company-page posts
        """
        body = {"initializeUploadRequest": {"owner": owner_urn}}
        resp = await self._request(
            "POST", "/rest/documents", params={"action": "initializeUpload"}, json=body,
        )
        data = resp.json().get("value", {})
        upload_url = data.get("uploadUrl")
        document_urn = data.get("document")
        if not upload_url or not document_urn:
            raise LinkedInError(f"register_document_upload: bad response {data}")
        return upload_url, document_urn

    async def upload_pdf(self, upload_url: str, pdf_path: str) -> None:
        """PUT raw PDF bytes to the upload_url returned by step 1.

        LinkedIn returns 201 No Content on success. The Authorization header
        is required even though the URL is single-use.
        """
        with open(pdf_path, "rb") as fh:
            data = fh.read()
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.put(
                upload_url,
                content=data,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        if r.status_code not in (200, 201):
            raise LinkedInError(
                f"upload_pdf -> {r.status_code}: {r.text[:300]}"
            )

    async def get_document_status(self, document_urn: str) -> str:
        """GET a document's processing status.

        Returns one of: WAITING_UPLOAD | PROCESSING | AVAILABLE |
        PROCESSING_FAILED. The post call needs the document AVAILABLE first.

        URNs in path variables must be URL-encoded (colons → %3A).
        Without encoding LinkedIn returns 400 ILLEGAL_ARGUMENT.
        """
        from urllib.parse import quote
        encoded = quote(document_urn, safe="")
        resp = await self._request("GET", f"/rest/documents/{encoded}")
        return resp.json().get("status", "")

    async def wait_for_document(
        self,
        document_urn: str,
        *,
        timeout_s: int = 120,
        poll_every_s: float = 3.0,
    ) -> None:
        """Poll until the document is AVAILABLE or we time out."""
        elapsed = 0.0
        while elapsed < timeout_s:
            status = await self.get_document_status(document_urn)
            if status == "AVAILABLE":
                return
            if status == "PROCESSING_FAILED":
                raise LinkedInError(
                    f"document {document_urn} processing failed"
                )
            await asyncio.sleep(poll_every_s)
            elapsed += poll_every_s
        raise LinkedInError(
            f"document {document_urn} did not become AVAILABLE in {timeout_s}s"
        )

    # -- Posts API ----------------------------------------------------------

    async def post_document(
        self,
        *,
        author_urn: str,
        commentary: str,
        document_urn: str,
        title: str,
    ) -> PostResult:
        """Create a document post on /rest/posts.

        Author URN can be a person or an organization; the API treats both
        the same as long as the caller has the matching scope and admin role.
        """
        body = {
            "author": author_urn,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {
                "media": {"id": document_urn, "title": title},
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        resp = await self._request("POST", "/rest/posts", json=body)
        post_urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
        if not post_urn:
            raise LinkedInError(
                f"post_document: no x-restli-id header. body: {resp.text[:300]}"
            )
        return PostResult(
            post_urn=post_urn,
            post_url=f"https://www.linkedin.com/feed/update/{post_urn}/",
        )

    async def post_text(
        self,
        *,
        author_urn: str,
        commentary: str,
    ) -> PostResult:
        """Plain-text post — no media. Used for X-equivalent shorter LI rows."""
        body = {
            "author": author_urn,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        resp = await self._request("POST", "/rest/posts", json=body)
        post_urn = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
        if not post_urn:
            raise LinkedInError(f"post_text: no x-restli-id header. body: {resp.text[:300]}")
        return PostResult(
            post_urn=post_urn,
            post_url=f"https://www.linkedin.com/feed/update/{post_urn}/",
        )

    async def delete_post(self, post_urn: str) -> None:
        """DELETE /rest/posts/{encoded-urn}. Returns 204 on success.

        Same scope as posting (w_member_social / w_organization_social).
        Idempotent — deleting an already-deleted post returns 204.
        """
        from urllib.parse import quote
        encoded = quote(post_urn, safe="")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                "DELETE",
                f"{API_HOST}/rest/posts/{encoded}",
                headers=self._headers(),
            )
        if resp.status_code not in (200, 204):
            raise LinkedInError(
                f"delete_post {post_urn} -> {resp.status_code}: {resp.text[:300]}"
            )

    # -- Comments / engagement on someone else's post ----------------------

    async def get_post(self, post_urn: str) -> dict:
        """GET /rest/posts/{encoded-urn}. Returns the full post JSON.

        Used to fetch a target post's commentary before drafting a reply
        comment on it. URN must be url-encoded by us — LinkedIn rejects
        otherwise (same reason wait_for_document had to encode).
        """
        from urllib.parse import quote
        encoded = quote(post_urn, safe="")
        resp = await self._request("GET", f"/rest/posts/{encoded}")
        return resp.json() or {}

    async def create_comment(
        self,
        *,
        post_urn: str,
        actor_urn: str,
        text: str,
        parent_comment_urn: str | None = None,
    ) -> str:
        """Post a comment on someone else's (or our own) LinkedIn post.

        actor_urn = whose name the comment is posted under:
          - urn:li:person:<id>          for founder profile     (w_member_social)
          - urn:li:organization:<id>    for company-page voice  (w_organization_social)

        Returns the new comment's URN (urn:li:comment:(<post>,<id>)).
        Raises LinkedInError on failure.
        """
        from urllib.parse import quote
        encoded = quote(post_urn, safe="")
        body: dict = {
            "actor": actor_urn,
            "object": post_urn,
            "message": {"text": text},
        }
        if parent_comment_urn:
            body["parentComment"] = parent_comment_urn
        resp = await self._request(
            "POST", f"/rest/socialActions/{encoded}/comments", json=body,
        )
        # The response either echoes the created comment as JSON or just
        # returns the new URN in a header — handle both.
        data = resp.json() if resp.content else {}
        comment_urn = (
            (data.get("id") if isinstance(data, dict) else None)
            or resp.headers.get("x-restli-id")
            or resp.headers.get("X-RestLi-Id")
        )
        if not comment_urn:
            raise LinkedInError(
                f"create_comment: no urn in response. body[:300]: {resp.text[:300]}"
            )
        return str(comment_urn)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def client_from_settings() -> LinkedInClient | None:
    """Build a LinkedInClient from current settings, or None if not configured."""
    s = settings()
    if not s.linkedin_access_token:
        return None
    return LinkedInClient(
        access_token=s.linkedin_access_token,
        api_version=s.linkedin_api_version,
        client_id=s.linkedin_client_id,
        client_secret=s.linkedin_client_secret,
        refresh_token=s.linkedin_refresh_token,
    )


def author_urn_for(brand_id: str) -> str | None:
    """Pick the right author URN for a given brand row.

    glitch_founder    -> Tejas's person URN
    glitch_executor   -> this project company URN
    other             -> None (caller falls back to Upload-Post)
    """
    s = settings()
    if brand_id == "glitch_founder":
        return s.linkedin_founder_person_urn or None
    if brand_id == "glitch_executor":
        return s.linkedin_brand_org_urn or None
    return None
