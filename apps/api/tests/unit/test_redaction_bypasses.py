"""S11 fix round 1: secret and personal values that once survived `redact()` and the JSON log
chain (LWA `Atza|` bearer tokens, `Authorization: Basic`, denied keys in JSON / repr / kwarg text,
the Postgres `Failing row contains (...)` DETAIL, and pydantic models or dataclasses passed as log
kwargs)."""

from __future__ import annotations

import pytest
from app.core.redaction import REDACTED, redact_text

SECRET = "Zq9" + "tail" * 4


@pytest.mark.parametrize(
    "text",
    [
        # Amazon LWA access tokens are `Atza|...`; `|` once stopped the Bearer token class.
        f"Authorization: Bearer Atza|{SECRET}",
        f"bearer Atzr|{SECRET}",
        f"Authorization: Basic {SECRET}",
        f'"authorization": "Token {SECRET}"',
    ],
)
def test_redact_text_hides_secret(text: str) -> None:
    out = redact_text(text)
    assert SECRET not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "token refreshed for tenant 7",
        "basic plan selected",
        '{"token_type": "bearer", "expires_in": 3600}',
        "{'code': 'NOT_FOUND', 'status': 404}",
        "tokens_in='120'",
    ],
)
def test_redact_text_keeps_harmless_text(text: str) -> None:
    assert redact_text(text) == text


def test_redact_text_is_idempotent() -> None:
    once = redact_text(f"Authorization: Bearer Atza|{SECRET}")
    assert redact_text(once) == once
