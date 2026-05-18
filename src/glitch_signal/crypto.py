"""Symmetric encryption + HMAC state-token helpers.

All platform refresh/access tokens in the `platform_auth` table are encrypted
at rest using Fernet (AES-128-CBC + HMAC-SHA256). A single `AUTH_ENCRYPTION_KEY`
env var drives both encryption and the HMAC used for OAuth state tokens.

Rotating the key requires re-running the OAuth handshake for every connected
account — existing rows become un-decryptable. That is intentional: it is
the simplest recovery path for a key compromise.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from functools import lru_cache

from cryptography.fernet import Fernet

from glitch_signal.config import settings


def _key_bytes() -> bytes:
    key = settings().auth_encryption_key
    if not key:
        raise RuntimeError(
            "AUTH_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return key.encode() if isinstance(key, str) else key


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_key_bytes())


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string. Returns a URL-safe base64 token."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token. Raises cryptography.fernet.InvalidToken on failure."""
    return _fernet().decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# OAuth state tokens — HMAC-signed, NOT encrypted. Short-lived.
# ---------------------------------------------------------------------------

_STATE_DEFAULT_TTL_S = 300   # 5 minutes


def make_state_token(payload: dict, ttl_s: int = _STATE_DEFAULT_TTL_S) -> str:
    """Build an HMAC-signed, URL-safe state token carrying `payload`.

    Format: `<base64url(payload)>.<base64url(hmac_sha256(payload))>`
    The payload is NOT encrypted — only signed — so never put secrets in it.
    """
    body = dict(payload)
    body["_exp"] = int(time.time()) + ttl_s
    body_b = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(
        hmac.new(_key_bytes(), body_b, hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{body_b.decode()}.{sig.decode()}"


def verify_state_token(token: str) -> dict:
    """Return the verified payload, or raise ValueError on any issue."""
    try:
        body_str, sig_str = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed state token") from exc

    body_b = body_str.encode()
    expected = base64.urlsafe_b64encode(
        hmac.new(_key_bytes(), body_b, hashlib.sha256).digest()
    ).rstrip(b"=").decode()

    if not hmac.compare_digest(sig_str, expected):
        raise ValueError("state token signature invalid")

    padding = "=" * (-len(body_str) % 4)
    try:
        body = json.loads(base64.urlsafe_b64decode(body_str + padding))
    except Exception as exc:
        raise ValueError(f"state token payload decode failed: {exc}") from exc

    if int(body.get("_exp", 0)) < int(time.time()):
        raise ValueError("state token expired")

    # Strip internal keys before returning to caller.
    return {k: v for k, v in body.items() if not k.startswith("_")}
