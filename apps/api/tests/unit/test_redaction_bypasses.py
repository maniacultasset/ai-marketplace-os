"""S11 fix round 1: secret and personal values that once survived `redact()` and the JSON log
chain (LWA `Atza|` bearer tokens, `Authorization: Basic`, denied keys in JSON / repr / kwarg text,
the Postgres `Failing row contains (...)` DETAIL, and pydantic models or dataclasses passed as log
kwargs)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import pytest
from app.core.redaction import REDACTED, redact_text
from pydantic import BaseModel, ValidationError
from tests.conftest import LogCapture

SECRET = "Zq9" + "tail" * 4


@pytest.mark.parametrize(
    "text",
    [
        # Amazon LWA access tokens are `Atza|...`; `|` once stopped the Bearer token class.
        f"Authorization: Bearer Atza|{SECRET}",
        f"bearer Atzr|{SECRET}",
        f"Authorization: Basic {SECRET}",
        f'"authorization": "Token {SECRET}"',
        # A token response or request payload quoted in an exception message or log text.
        f'LWA refresh failed: {{"access_token": "Atza|{SECRET}", "token_type": "bearer"}}',
        f"LWA refresh failed: {{'refresh_token': 'Atzr|{SECRET}'}}",
        f"Settings(jwt_secret='{SECRET}')",
        f'client_secret: "{SECRET}"',
        f"OAuthError(client_secret='{SECRET}')",
        f"{{'email': '{SECRET}@x.example',...ess_token': '{SECRET}'}}",
        # An unterminated quoted value (truncated text) still loses its value.
        f"{{'access_token': '{SECRET}",
        # Round 2: a Python bytes repr after a denied key (`b'` / `b"` opens the value).
        f"{{'token_hash': b'\\x01{SECRET}', 'tenant_id': 3}}",
        f"MarketplaceToken(token_enc=b'{SECRET}')",
        f"MarketplaceToken(id=1, token_enc=b'{SECRET}')",
        f"{{'state_hash': b\"{SECRET}\"}}",
        # Round 2: a backslash-escaped quote inside the value does not close it.
        f'{{"password": "ab\\"{SECRET}"}}',
        f'{{"client_secret": "x\\"{SECRET}", "grant_type": "refresh_token"}}',
        f'payload="{{\\"password\\": \\"ab\\\\\\"{SECRET}\\"}}"',
    ],
)
def test_redact_text_hides_secret(text: str) -> None:
    out = redact_text(text)
    assert SECRET not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        # psycopg CheckViolation / NotNullViolation text, as str(exc) and SQLAlchemy carry it.
        'new row for relation "t" violates check constraint "t_b_check"\n'
        f"DETAIL:  Failing row contains ({SECRET}@b.example, -1).",
        'null value in column "n" of relation "users" violates not-null constraint\n'
        f"DETAIL:  Failing row contains (1, {SECRET}@x.example, $argon2id${SECRET}, null).\n"
        "[SQL: INSERT INTO users ...]",
    ],
)
def test_redact_text_hides_failing_row(text: str) -> None:
    out = redact_text(text)
    assert SECRET not in out
    assert f"Failing row contains ({REDACTED})" in out
    assert "violates" in out


@pytest.mark.parametrize(
    "text",
    [
        "token refreshed for tenant 7",
        "basic plan selected",
        '{"token_type": "bearer", "expires_in": 3600}',
        "{'code': 'NOT_FOUND', 'status': 404}",
        "tokens_in='120'",
        "{'code': b'NOT_FOUND', 'status': 404}",
        '{"note": "say \\"hi\\"", "status": 200}',
    ],
)
def test_redact_text_keeps_harmless_text(text: str) -> None:
    assert redact_text(text) == text


def test_quoted_value_after_escaped_quote_keeps_following_keys() -> None:
    out = redact_text(f'{{"password": "ab\\"{SECRET}", "grant_type": "refresh_token"}}')
    assert SECRET not in out
    assert '"grant_type": "refresh_token"' in out


def test_redact_text_is_idempotent() -> None:
    once = redact_text(f"Authorization: Bearer Atza|{SECRET} {{'access_token': '{SECRET}'}}")
    assert redact_text(once) == once


def test_validation_error_input_value_redacted() -> None:
    class Payload(BaseModel):
        email: str
        password: str
        access_token: str
        required: int

    with pytest.raises(ValidationError) as caught:
        Payload.model_validate(
            {"email": f"{SECRET}@x.example", "password": SECRET, "access_token": SECRET}
        )
    assert SECRET not in redact_text(str(caught.value))


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


@dataclasses.dataclass
class TokenPair:
    access_token: str
    token_type: str


def test_object_kwarg_redacted_in_log_line(log_lines: Callable[..., LogCapture]) -> None:
    """A pydantic model or dataclass passed as a log kwarg goes through the key layer."""
    from app.core.logging import get_logger

    capture = log_lines()
    get_logger("app.tests.redaction").info(
        "token refreshed",
        response=TokenResponse(access_token=SECRET, refresh_token=SECRET, expires_in=3600),
        pair=TokenPair(access_token=SECRET, token_type="bearer"),
    )
    [line] = capture.lines()
    assert SECRET not in json.dumps(line)
    assert line["response"] == {
        "access_token": REDACTED,
        "refresh_token": REDACTED,
        "expires_in": 3600,
    }
    assert line["pair"] == {"access_token": REDACTED, "token_type": "bearer"}
