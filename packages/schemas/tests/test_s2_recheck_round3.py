"""Round 3 re-check of the seam secret walk (AC 17): cases the round 2 fix must keep holding."""

from __future__ import annotations

import json
import time

import pytest
from mkos_schemas.base import SEAM_REDACTED, find_seam_secret_key
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem

_SECRET = "sk-LIVE-ROUND2-SECRET-VALUE"


def _item(**overrides: object) -> ErrorItem:
    base: dict[str, object] = {
        "field": "listing",
        "error_code": next(iter(ErrorCode)),
        "message": "m",
        "source": "amazon_adapter",
        "retryable": False,
    }
    base.update(overrides)
    return ErrorItem.model_validate(base)


@pytest.mark.parametrize(
    "text",
    [
        "﻿" + json.dumps(json.dumps({"access_token": _SECRET})),
        "  " + json.dumps(json.dumps(json.dumps({"apiKey": _SECRET}))) + "\n",
        'list: ["ok", ' + json.dumps(json.dumps({"password": _SECRET})) + "]",
        'outer {"body": ' + json.dumps(json.dumps([{"X-Api-Key": _SECRET}])) + "}",
    ],
)
def test_bom_and_nested_double_encoded_json_is_redacted(text: str) -> None:
    assert find_seam_secret_key(text) is not None
    dumped = _item(message=text, suggestion=text, value_submitted=text).model_dump_json()
    assert _SECRET not in dumped
    assert dumped.count(SEAM_REDACTED) == 3


def test_scan_cost_stays_bounded_on_hostile_text() -> None:
    # 64 candidates that each parse a long unterminated array, in many list items.
    hostile = [("[1," * 5000 + "x ") * 64] * 50
    started = time.perf_counter()
    for text in hostile:
        find_seam_secret_key(text)
    assert time.perf_counter() - started < 5.0
