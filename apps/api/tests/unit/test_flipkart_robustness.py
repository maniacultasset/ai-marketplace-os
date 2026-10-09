"""S22 tester repros, fixed: a deeply nested JSON reply is a protocol error, never a
RecursionError; update_listing refuses an unsafe seller SKU as VALUE_NOT_ALLOWED (invariant 8)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.adapters.base import MarketplaceAmbiguous, MarketplaceUnavailable
from tests.adapters.flipkart_support import fk_item, flipkart_adapter, flipkart_client
from tests.support.fake_marketplace import build_fake

LISTING = {"sku": "SKU-1", "vertical": "kurta", "attributes": {"title": [{"value": "K"}]}}


def _mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("depth", [100_000])
async def test_flipkart_deeply_nested_json_is_protocol_error(depth: int) -> None:
    """A 2xx body of deeply nested JSON (well under the 1 MiB cap) must be a protocol error
    (ambiguous after a write), never an uncaught RecursionError out of the adapter."""
    app = build_fake()
    body = b"[" * depth + b"]" * depth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = flipkart_client(app, transport=_mock(handler))
    with pytest.raises(MarketplaceAmbiguous):
        await client.create([LISTING])
    client = flipkart_client(app, transport=_mock(handler))
    with pytest.raises(MarketplaceUnavailable):
        await client.get(["SKU-1"])


@pytest.mark.parametrize("sku", ["A,B", "A/B", ".."])
async def test_update_unsafe_sku_is_value_not_allowed(sku: str) -> None:
    """R4: create refuses an unsafe SKU as a VALUE_NOT_ALLOWED result (AC10), but update_listing
    lets the client's bare ValueError escape (no ErrorReport, invariant 8)."""
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_item(sku=sku)
    created = await adapter.create_listing(sent)
    assert created.error is not None and created.error.errors[0].error_code == "VALUE_NOT_ALLOWED"
    out = await adapter.update_listing(sent, fields={sku: ["content.title"]})
    assert out.status == "failed"
    assert out.error is not None
    assert any(e.error_code == "VALUE_NOT_ALLOWED" for e in out.error.errors)
    assert app.state.fake.calls == []


# R2/R3 (create/update/get errors not a list) and the reply-shape property moved to
# apps/api/tests/unit/test_flipkart_reply_shape.py (fix round 2, F1).
