"""S11 T4: `redact()` for logs and Sentry events (keys, value patterns, bounded time)."""

from __future__ import annotations

import copy
import json
import math
import time
from typing import Any

import pytest
from app.core.redaction import (
    DENY_KEYS,
    MAX_STR_CHARS,
    QUERY_DENY_KEYS,
    REDACTED,
    TRUNCATED_MARKER,
    is_denied_key,
    redact,
)
from hypothesis import given
from hypothesis import strategies as st

NAMED_KEYS = [
    "AMAZON_CLIENT_SECRET",
    "access_token",
    "refresh_token",
    "hashed_password",
    "token_enc",
    "Authorization",
    "AUTHORIZATION",
    "set-cookie",
    "cookie",
    "x-api-key",
    "email",
    "ip",
    "actor_email",
    "login_email",
    "token_hash",
    "state_hash",
    "code_verifier",
    "gstin",
    "user_agent",
    "x-forwarded-for",
    "contact_email",
    "client_ip",
    "refresh_token_hash",
    "db_scram",
    "password",
    "phone",
    "jwt_secret",
    "sentry_dsn",
    "database_url",
    "s3_secret_key",
    "kms_local_dev_key",
    "alert_slack_webhook",
    "openai_api_key",
    "admin_session_secret",
    "secret",
    "api_key",
    "mws_auth_token",
    "admin_staff_emails",
]


@pytest.mark.parametrize("key", NAMED_KEYS)
def test_redacts_named_keys(key: str) -> None:
    assert is_denied_key(key)
    out = redact({key: "Zq9value", "keep": "visible"})
    assert out == {key: REDACTED, "keep": "visible"}


def test_redacts_non_string_values_under_denied_keys() -> None:
    data: dict[str, Any] = {"password": 12345, "token": ["a", "b"], "email": None}
    out = redact(data)
    assert out == {"password": REDACTED, "token": REDACTED, "email": None}


def test_redacts_nested() -> None:
    data = {"outer": [{"inner": {"access_token": "Zq9"}}, ("x", {"email": "a@b.example"})]}
    out = redact(data)
    assert out == {"outer": [{"inner": {"access_token": REDACTED}}, ("x", {"email": REDACTED})]}
    assert isinstance(out["outer"][1], tuple)


def test_redacts_header_pairs() -> None:
    headers = [["authorization", "Bearer x"], ["accept", "text/plain"], ("Cookie", "s=Zq9")]
    out = redact(headers)
    assert out == [["authorization", REDACTED], ["accept", "text/plain"], ("Cookie", REDACTED)]
    raw: list[Any] = redact([(b"authorization", b"Bearer Zq9")])
    assert raw == [(b"authorization", REDACTED)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def-ghi", f"Authorization: Bearer {REDACTED}"),
        ("bearer abc", f"Bearer {REDACTED}"),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end",
            f"token {REDACTED} end",
        ),
        ("postgresql://u:Zq9@h/db", f"postgresql://u:{REDACTED}@h/db"),
        ("https://Zq9tok@github.example/x", f"https://{REDACTED}@github.example/x"),
        (
            "https://h/cb?access_token=Zq9&x=1",
            f"https://h/cb?access_token={REDACTED}&x=1",
        ),
        (
            "spapi_oauth_code=Zq9&state=Zq8&mws_auth_token=Zq7",
            f"spapi_oauth_code={REDACTED}&state={REDACTED}&mws_auth_token={REDACTED}",
        ),
        ("https://h/cb?code=Zq6", f"https://h/cb?code={REDACTED}"),
        ("GET /x?password=Zq5 HTTP/1.1", f"GET /x?password={REDACTED} HTTP/1.1"),
    ],
)
def test_redacts_value_patterns(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert "Zq" not in redact({"msg": text})["msg"]


@pytest.mark.parametrize(
    ("text", "columns"),
    [
        (
            "DETAIL:  Key (lower(email::text))=(a@b.example) already exists.",
            "lower(email::text)",
        ),
        ("Key (tenant_id, sku)=(0b6e-11, Zq9) already exists.", "tenant_id, sku"),
        ('Key (tenant_id)=(Zq9) is not present in table "tenants".', "tenant_id"),
        ("Key (code)=(Zq9 with) paren) already exists.", "code"),
        ("Key (code)=(Zq9 unterminated", "code"),
    ],
)
def test_redacts_unique_violation_detail(text: str, columns: str) -> None:
    out = redact(text)
    assert f"Key ({columns})=({REDACTED})" in out
    assert "a@b" not in out
    assert "Zq9" not in out


def test_redacts_sqlalchemy_error_parameters() -> None:
    text = (
        "(psycopg.errors.UniqueViolation) duplicate key value violates unique constraint\n"
        "[SQL: INSERT INTO users (email, hashed_password) VALUES (%(email)s, %(hp)s)]\n"
        "[parameters: {'email': 'a@b.example', 'hp': '$argon2id$Zq9'}]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    out = redact(text)
    assert f"[parameters: {REDACTED}]\n" in out
    assert "a@b" not in out
    assert "Zq9" not in out
    assert "[SQL: INSERT INTO users (email, hashed_password)" in out
    assert redact(out) == out


@pytest.mark.parametrize(
    "value",
    [
        {"tokens_in": 120, "tokens_out": 30},
        {"request_id": "abc", "idempotency_key": "k-1"},
        {"code": "NOT_FOUND", "state": "draft"},
        "Bearer",
        "eyJ",
        "https://example.invalid/path",
        "/api/v1/products?page=2",
        "status=ok&limit=5",
    ],
)
def test_keeps_safe_fields(value: Any) -> None:
    assert redact(value) == value


def test_boundaries() -> None:
    assert redact({}) == {}
    assert redact([]) == []
    assert redact("") == ""
    assert redact({"ключ": "значение", "emoji": "🙂"}) == {"ключ": "значение", "emoji": "🙂"}
    assert redact({1: "x", None: "y"}) == {1: "x", None: "y"}
    out = redact({"n": float("nan"), "i": float("inf")})
    assert math.isnan(out["n"])
    assert out["i"] == float("inf")
    deep: Any = "leaf"
    for _ in range(40):
        deep = {"d": deep}
    capped = redact(deep)
    node = capped
    depth = 0
    while isinstance(node, dict):
        node = node["d"]
        depth += 1
    assert node == REDACTED
    assert depth <= 32


def test_truncates_long_strings() -> None:
    head, middle, tail = "H" * 4_096, "M" * 8_000, "T" * (12_288 - 11) + " Bearer Zq9"
    text = head + middle + tail
    assert len(text) > 20_000
    out = redact(text)
    # The cut keeps MAX_STR_CHARS characters; the one replacement may lengthen the tail.
    assert len(out) <= MAX_STR_CHARS + len(TRUNCATED_MARKER) + len(REDACTED)
    assert out.startswith(head + TRUNCATED_MARKER)
    assert "M" not in out
    assert "Zq9" not in out
    assert out.endswith(f"Bearer {REDACTED}")
    exact = "y" * MAX_STR_CHARS
    assert redact(exact) == exact


ADVERSARIAL = {
    "userinfo_scheme": "a://" * 250_000,
    "userinfo_colon": "x:" * 500_000,
    "bearer": "Bearer " * 150_000,
    "jwt": "eyJ." * 250_000,
    "query": "&a=" * 330_000,
    "detail": "Key (" * 200_000,
    "detail_unterminated": "Key (a)=(" + "z" * 1_000_000,
    "detail_repeated": "Key (a)=(" * 110_000,
    "sql_parameters": "[parameters: " * 80_000,
    "failing_row": "Failing row contains (" * 50_000,
    "input_value": "input_value=x" * 80_000,
    "quoted_key": "'access_token': '" * 60_000,
    "quoted_key_unterminated": "'secret': '" + "z" * 1_000_000,
    "quoted_escaped": '\\"a\\": \\"' * 100_000,
    "auth_scheme": "Authorization: Basic " * 50_000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_redaction_linear_time_adversarial(name: str) -> None:
    text = ADVERSARIAL[name]
    started = time.perf_counter()
    redact({"message": text, "items": [text]})
    assert time.perf_counter() - started < 0.1


def test_input_not_mutated() -> None:
    data = {"a": [{"password": "Zq9"}], "h": [["authorization", "x"]], "s": "Bearer Zq9"}
    before = copy.deepcopy(data)
    redact(data)
    assert data == before


def test_redaction_covers_every_secret_setting() -> None:
    from app.core.config import Settings

    hidden = [name for name, field in Settings.model_fields.items() if field.repr is False]
    assert hidden, "Settings has repr=False fields"
    for name in hidden:
        assert is_denied_key(name), name
        assert is_denied_key(name.upper()), name


@pytest.mark.parametrize(
    "column",
    [
        "token_enc",
        "hashed_password",
        "token_hash",
        "state_hash",
        "code_verifier",
        "email",
        "actor_email",
        "phone",
        "gstin",
        "ip",
        "user_agent",
    ],
)
def test_redaction_covers_schema_columns(column: str) -> None:
    assert is_denied_key(column)


def test_query_deny_keys_extend_deny_keys() -> None:
    assert DENY_KEYS < QUERY_DENY_KEYS
    assert {"code", "state", "spapi_oauth_code", "mws_auth_token"} <= QUERY_DENY_KEYS
    assert not is_denied_key("code")
    assert not is_denied_key("state")


_JSON_LEAF = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False),
    st.text(max_size=200),
)
_JSON = st.recursive(
    _JSON_LEAF,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=20), children, max_size=5),
    ),
    max_leaves=30,
)


@given(_JSON)
def test_redact_idempotent_and_total(value: Any) -> None:
    once = redact(value)
    assert redact(once) == once


@given(
    key=st.sampled_from(sorted(DENY_KEYS)),
    secret=st.text(alphabet=st.characters(categories=("L", "N")), min_size=6, max_size=40).map(
        lambda s: "Zq9" + s
    ),
    other=_JSON,
)
def test_planted_secret_never_survives(key: str, secret: str, other: Any) -> None:
    payload = {"other": other, "nested": [{key: secret}], key.upper(): secret}
    assert secret not in json.dumps(redact(payload), ensure_ascii=False)
