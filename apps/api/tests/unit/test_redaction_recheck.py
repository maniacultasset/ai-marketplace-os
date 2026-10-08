"""S11 re-check round 2: the round-1 redaction fixes hold on the paths that carry text to stdout
and to Sentry (stdlib %-args, an exception traceback, a Sentry exception value), nested model
kwargs, and adversarial input stays bounded in time."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from app.core.observability import before_send
from app.core.redaction import MAX_STR_CHARS, REDACTED, redact, redact_text
from pydantic import BaseModel
from tests.conftest import LogCapture
from tests.timing import budget

SECRET = "Qx7" + "leak" * 4
LWA = f"Atza|IwEB{SECRET}|x.y-z"
TOKEN_BODY = f'{{"access_token": "{LWA}", "refresh_token": "Atzr|{SECRET}", "expires_in": 3600}}'


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int


def test_stdlib_percent_args_redacted(log_lines: Callable[..., LogCapture]) -> None:
    import logging

    capture = log_lines()
    logging.getLogger("app.tests.recheck").warning(
        "lwa refresh failed: %s %s", TOKEN_BODY, f"Bearer {LWA}"
    )
    text = capture.text()
    assert SECRET not in text
    assert "IwEB" not in text
    assert "expires_in" in text


def test_exception_traceback_redacted(log_lines: Callable[..., LogCapture]) -> None:
    from app.core.logging import get_logger

    capture = log_lines()
    try:
        raise RuntimeError(f"token endpoint said {TOKEN_BODY}; header Authorization: Bearer {LWA}")
    except RuntimeError:
        get_logger("app.tests.recheck").exception("unhandled")
    [line] = capture.lines()
    dumped = json.dumps(line)
    assert SECRET not in dumped
    assert "RuntimeError" in dumped


def test_nested_model_kwargs_redacted(log_lines: Callable[..., LogCapture]) -> None:
    from app.core.logging import get_logger

    capture = log_lines()
    get_logger("app.tests.recheck").info(
        "batch",
        items=[TokenResponse(access_token=SECRET, expires_in=1)],
        by_shop={"s1": TokenResponse(access_token=SECRET, expires_in=2)},
    )
    [line] = capture.lines()
    assert SECRET not in json.dumps(line)
    assert line["items"] == [{"access_token": REDACTED, "expires_in": 1}]
    assert line["by_shop"] == {"s1": {"access_token": REDACTED, "expires_in": 2}}


def test_sentry_exception_value_redacted() -> None:
    event: dict[str, Any] = {
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": (
                        'null value in column "is_active" violates not-null constraint\n'
                        f"DETAIL:  Failing row contains (1, {SECRET}@x.test, $argon2id${SECRET})."
                    ),
                },
                {"type": "HTTPStatusError", "value": f"lwa: {TOKEN_BODY}"},
            ]
        },
        "logentry": {"message": "auth %s", "params": [f"Bearer {LWA}"]},
    }
    assert SECRET not in json.dumps(before_send(event, {}))


@pytest.mark.parametrize(
    "text",
    [
        "'password': '" * 4000,
        '"access_token": "' * 4000,
        "input_value=" + "," * 20000,
        "Failing row contains (" * 2000,
        "Key (" * 5000,
        "Bearer " * 5000,
        "a='" + "x" * 50000,
        ("x" * 64 + "=") * 1000,
        "\\\\'" * 10000 + ":",
    ],
)
def test_adversarial_text_is_bounded(text: str) -> None:
    started = time.perf_counter()
    out = redact_text(text)
    assert time.perf_counter() - started < budget(1.0)  # machine-scaled (tests/timing.py)
    assert len(out) <= MAX_STR_CHARS + 64 * 1000


def test_redact_never_mutates_input() -> None:
    value = {"note": TOKEN_BODY, "list": [f"Bearer {LWA}"]}
    snapshot = json.dumps(value)
    out = redact(value)
    assert json.dumps(value) == snapshot
    assert SECRET not in json.dumps(out)
