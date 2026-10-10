"""Statuses and headers the running API answers are declared in openapi.json (audit
GAP2-CONTRACT-DOCS-CONFORMANCE-2): `Retry-After` on every 429.
No database: the spec is built from a fresh `create_app()`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI

PREFIX = "/api/v1"


@pytest.fixture
def spec(make_app: Callable[[], FastAPI]) -> dict[str, Any]:
    return make_app().openapi()


def _ops(spec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        for method, op in item.items():
            yield f"{method.upper()} {path}", op


def test_every_429_declares_retry_after(spec: dict[str, Any]) -> None:
    missing = [
        name
        for name, op in _ops(spec)
        if "429" in op["responses"]
        and "Retry-After" not in op["responses"]["429"].get("headers", {})
    ]
    assert missing == []
    header = spec["paths"][PREFIX + "/products"]["get"]["responses"]["429"]["headers"]
    assert header["Retry-After"]["schema"] == {"type": "integer", "minimum": 1}
