"""Trusted-browser tokens (ADR-0023): 256 random bits as 43 base64url characters.

The browser keeps the raw token and sends it only in a JSON body; the server stores only its
domain-separated sha256, bound to one user, and swaps the value on every use. A token is never
logged (the redaction keys `device_token`, `device_tokens`, `token_hash` and `prev_token_hash`).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Final

TOKEN_BYTES: Final = 32
TOKEN_CHARS: Final = 43
_CONTEXT: Final = b"mkos-trusted-device|"
_SHAPE: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def new_token() -> str:
    """A fresh token: `secrets.token_urlsafe(32)` (43 characters, no padding)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def well_formed(token: str) -> bool:
    """True for exactly 43 base64url characters; anything else never reaches the database."""
    return isinstance(token, str) and _SHAPE.fullmatch(token) is not None


def digest(token: str) -> bytes:
    """The stored form: sha256 over a fixed context and the token (32 bytes)."""
    return hashlib.sha256(_CONTEXT + token.encode("ascii")).digest()


def matches(stored: bytes, presented: bytes | None) -> bool:
    """A constant-time comparison of two digests."""
    return presented is not None and hmac.compare_digest(stored, presented)


__all__ = ["TOKEN_CHARS", "digest", "matches", "new_token", "well_formed"]
