"""Time-based one-time passwords (RFC 6238 over RFC 4226 HOTP), standard library only (DPP-1).

HMAC-SHA1, 6 digits, 30-second steps, a window of one step either side, 160-bit secrets: the
parameters every authenticator app reads from a plain `otpauth://totp/` URI. `match_step` returns
the step that matched so the caller can make each code single use (`last_used_step`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from typing import Final
from urllib.parse import quote, urlencode

STEP_SECONDS: Final = 30
DIGITS: Final = 6
WINDOW: Final = 1
SECRET_BYTES: Final = 20
MFA_ISSUER: Final = "Kuinn"

_ASCII_DIGITS: Final = frozenset("0123456789")


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def b32(secret: bytes) -> str:
    """RFC 4648 base32, upper case, with the `=` padding stripped (what authenticator apps take)."""
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def step_at(unix: float) -> int:
    return int(unix // STEP_SECONDS)


def hotp(secret: bytes, counter: int) -> str:
    """RFC 4226 §5.3: HMAC-SHA1 of the 8-byte counter, dynamic truncation, zero-padded digits."""
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def _well_formed(code: object) -> bool:
    return isinstance(code, str) and len(code) == DIGITS and all(ch in _ASCII_DIGITS for ch in code)


def match_step(secret: bytes, code: str, now: float) -> int | None:
    """The step in t-1..t+1 whose code equals `code`, or None.

    Every step in the window is compared with `hmac.compare_digest`, with no early exit, so the
    time taken does not say which step matched. A code that is not exactly 6 ASCII digits is None
    before any HMAC runs.
    """
    if not _well_formed(code):
        return None
    current = step_at(now)
    matched: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        same = hmac.compare_digest(hotp(secret, step).encode("ascii"), code.encode("ascii"))
        if same and matched is None:
            matched = step
    return matched


def otpauth_uri(secret: bytes, *, account: str, issuer: str = MFA_ISSUER) -> str:
    """The Key Uri Format URI an authenticator app scans; the label is `issuer:account`, quoted."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    query = urlencode(
        {
            "secret": b32(secret),
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": str(DIGITS),
            "period": str(STEP_SECONDS),
        },
        quote_via=quote,
    )
    return f"otpauth://totp/{label}?{query}"
