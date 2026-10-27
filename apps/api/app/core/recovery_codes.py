"""MFA recovery codes (DPP-1): 10 single-use codes of 128 bits each, shown once.

A code is 16 random bytes as lower-case base32 without padding (26 characters), shown in groups of
four joined by `-`. Only `sha256(user_id || normalised code)` is stored, so a stolen table row
works for no other user and reveals nothing about the code.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Final
from uuid import UUID

COUNT: Final = 10
CODE_BYTES: Final = 16
CODE_CHARS: Final = 26
_ALPHABET: Final = frozenset("abcdefghijklmnopqrstuvwxyz234567")
_IGNORED: Final = frozenset(" -")
_MAX_RAW: Final = 64


def display(raw: bytes) -> str:
    """16 random bytes as the grouped form the user sees, e.g. `abcd-efgh-...-xy`."""
    plain = base64.b32encode(raw).decode("ascii").rstrip("=").lower()
    return "-".join(plain[i : i + 4] for i in range(0, len(plain), 4))


def new_codes() -> list[str]:
    """`COUNT` distinct codes from `secrets.token_bytes`; a repeated draw is drawn again."""
    codes: list[str] = []
    while len(codes) < COUNT:
        code = display(secrets.token_bytes(CODE_BYTES))
        if code not in codes:
            codes.append(code)
    return codes


def normalise(raw: str) -> str | None:
    """The 26-character form of a typed code (case, spaces and dashes ignored), or None when it
    cannot be a code, so a malformed value never reaches the database."""
    if not isinstance(raw, str) or len(raw) > _MAX_RAW:
        return None
    lowered = "".join(ch for ch in raw.lower() if ch not in _IGNORED)
    if len(lowered) != CODE_CHARS or any(ch not in _ALPHABET for ch in lowered):
        return None
    return lowered


def code_hash(user_id: UUID, normalised: str) -> bytes:
    return hashlib.sha256(user_id.bytes + normalised.encode("ascii")).digest()
