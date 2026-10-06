"""S11 re-check round 4: container values (list, tuple, set, dict, bytearray) after a denied key
in repr, JSON and kwarg text, attacked with quotes, brackets, escapes and separators inside."""

from __future__ import annotations

import json

import pytest
from app.core.redaction import REDACTED, redact_text

SECRET = "Kx7" + "lane" * 4


@pytest.mark.parametrize(
    "text",
    [
        f"admin_staff_emails = [ '{SECRET}@x.example' ]",
        f"admin_staff_emails: ['{SECRET}']",
        f"?x=1&admin_staff_emails=['{SECRET}']",
        repr({"cc_emails": {f"{SECRET}@x"}}),
        repr({"cc_emails": ["]", SECRET]}),
        repr({"cc_emails": ["he said \"hi'", SECRET]}),
        repr({"cc_emails": ["a\\", SECRET]}),
        repr({"cc_emails": ["é]'\"", SECRET]}),
        repr({"cookies": {"a": [SECRET, {"b": "]"}]}}),
        repr({"token_hash": [SECRET.encode(), b"]'"]}),
        json.dumps(json.dumps({"login_email": ["a]b", SECRET]})),
        json.dumps({"login_email": ['"]', SECRET]}),
    ],
)
def test_container_value_attack_is_redacted(text: str) -> None:
    assert SECRET not in redact_text(text)


def test_container_with_quoted_bracket_keeps_following_keys() -> None:
    out = redact_text(repr({"cc_emails": ["]", SECRET], "status": "ok"}))
    assert out == f"{{'cc_emails': {REDACTED}, 'status': 'ok'}}"


def test_redacted_container_is_idempotent() -> None:
    once = redact_text(f"?a=1&admin_staff_emails=['{SECRET}'] {{'cc_emails': ('{SECRET}',)}}")
    assert SECRET not in once
    assert redact_text(once) == once
