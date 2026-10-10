"""Statuses and headers the running API answers are declared in openapi.json (audit
GAP2-CONTRACT-DOCS-CONFORMANCE-2, -4, -5): `Retry-After` on every 429, the OAuth callback's 502
and 503, `/readyz`'s 503, and the global body-cap middleware's 413 and 400 on every operation.
No database: the spec is built from a fresh `create_app()`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from app.api.errors import STATUS_MESSAGES
from app.api.v1.surface import OPS
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


def test_callback_declares_its_upstream_statuses(spec: dict[str, Any]) -> None:
    responses = spec["paths"][PREFIX + "/marketplaces/{marketplace}/callback"]["get"]["responses"]
    assert "MARKETPLACE_UNAVAILABLE" in responses["503"]["description"]
    assert "MARKETPLACE_UNKNOWN_ERROR" in responses["502"]["description"]
    for status in ("502", "503"):
        example = responses[status]["content"]["application/json"]["example"]["error"]
        assert example["message"] != STATUS_MESSAGES[500], status


def test_upstream_statuses_have_their_own_message() -> None:
    assert STATUS_MESSAGES[502] != STATUS_MESSAGES[500]
    assert STATUS_MESSAGES[503] != STATUS_MESSAGES[500]


def test_readyz_declares_503(spec: dict[str, Any]) -> None:
    responses = spec["paths"]["/readyz"]["get"]["responses"]
    example = responses["503"]["content"]["application/json"]["example"]
    assert example == {"status": "unavailable", "checks": {"postgres": "unavailable"}}


@pytest.mark.parametrize("status", ["400", "413"])
def test_body_cap_statuses_declared_on_every_operation(spec: dict[str, Any], status: str) -> None:
    """BodyLimitMiddleware runs before routing: a body over the cap is 413 and a malformed
    Content-Length is 400 on any operation, with or without a declared body."""
    missing = [
        name
        for name, op in _ops(spec)
        if name.split()[1].startswith(PREFIX) and status not in op["responses"]
    ]
    assert missing == []
    assert len(OPS) == sum(1 for name, _ in _ops(spec) if name.split()[1].startswith(PREFIX))


def test_default_response_names_only_reachable_codes(spec: dict[str, Any]) -> None:
    for name, op in _ops(spec):
        if not name.split()[1].startswith(PREFIX):
            continue
        text = op["responses"]["default"]["description"]
        assert "until the owner step" not in text, name
