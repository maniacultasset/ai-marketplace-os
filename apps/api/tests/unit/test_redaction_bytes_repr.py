"""Tester re-check (round 3): real Python reprs of bytes values under denied keys are redacted.

Builds the text with `repr()` / f-string `!r` so the escaping is exactly what a log message,
exception text or traceback would carry, rather than hand-written literals.
"""

from __future__ import annotations

import pytest
from app.core.redaction import redact_text

SECRET = b"Zq9" + b"tail" * 4


@pytest.mark.parametrize(
    "value",
    [
        SECRET,
        b"\x00\x01\xff" + SECRET,
        b"'" + SECRET,  # repr switches to double quotes
        b"'\"" + SECRET,  # repr keeps single quotes and escapes them
        SECRET + b"\\",  # trailing backslash: repr ends in `\\'`
        b"\\'" + SECRET,
        b"\n\t" + SECRET,
    ],
)
@pytest.mark.parametrize("key", ["token_hash", "token_enc", "state_hash", "Token-Hash"])
def test_bytes_repr_under_denied_key_is_redacted(key: str, value: bytes) -> None:
    for text in (
        repr({key: value, "tenant_id": 3}),
        f"MarketplaceToken(id=1, {key.replace('-', '_')}={value!r}, tenant_id=3)",
        f"IntegrityError: { ({key: value})!r}",
    ):
        out = redact_text(text)
        assert SECRET.decode() not in out, out
        assert "tenant_id" in out or "IntegrityError" in out


def test_bytes_repr_redaction_keeps_neighbour_fields() -> None:
    out = redact_text(repr({"token_hash": b"\\" + SECRET + b"'", "tenant_id": 3, "code": b"OK"}))
    assert SECRET.decode() not in out
    assert out.endswith("'tenant_id': 3, 'code': b'OK'}")


def test_harmless_bytes_repr_unchanged() -> None:
    text = repr({"code": b"NOT_FOUND", "request_id": b"abc", "status": 404})
    assert redact_text(text) == text
