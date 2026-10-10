"""S22 tester attacks (round 1): the Flipkart client and adapter against hostile replies, over
`httpx.MockTransport` and the in-process fake (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.adapters.base import MarketplaceAmbiguous, MarketplaceUnavailable
from tests.adapters.flipkart_support import (
    FakeTokens,
    fk_family,
    fk_item,
    flipkart_adapter,
    flipkart_client,
)
from tests.support.fake_marketplace import build_fake

LISTING = {"sku": "SKU-1", "vertical": "kurta", "attributes": {"title": [{"value": "K"}]}}


def _mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_flipkart_whole_request_400_echoing_token_is_scrubbed() -> None:
    """A 400 for the whole request whose description echoes the bearer token: every SKU's
    ErrorItem message must not hold the token (the D-10 scrub on the whole-request path)."""
    app = build_fake()
    tokens = FakeTokens(app)
    secret = tokens.token

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"].split(" ", 1)[1]
        return httpx.Response(
            400,
            json={
                "errors": [
                    {"code": "BAD_REQUEST", "description": f"bad token {auth}", "severity": "ERROR"}
                ]
            },
        )

    adapter = flipkart_adapter(app, transport=_mock(handler), tokens=tokens)
    result = await adapter.create_listing(fk_family())
    assert result.status == "failed"
    dumped = result.model_dump_json()
    assert secret not in dumped
    for one in result.items:
        assert one.marketplace_state.value == "rejected"


async def test_flipkart_results_for_foreign_sku_are_ignored() -> None:
    """A 2xx answer that names a SKU we did not send and omits ours: ours is failed, never
    marked succeeded from someone else's row."""
    app = build_fake()
    sent = fk_item(sku="OURS-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"request_id": "r1", "results": [{"sku": "THEIRS-1", "status": "SUCCESS"}]},
        )

    adapter = flipkart_adapter(app, transport=_mock(handler))
    result = await adapter.create_listing(sent)
    assert result.status == "failed"
    assert result.items[0].external_id is None
    assert all(f.status != "succeeded" for f in result.items[0].fields)


async def test_flipkart_non_dict_results_and_errors_do_not_crash() -> None:
    """Junk shapes in a 2xx answer (results rows that are strings, errors as a dict) give a
    failed item, never a TypeError."""
    app = build_fake()
    sent = fk_item(sku="OURS-2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    "junk",
                    {"sku": "OURS-2", "status": "FAILURE", "errors": {"code": "X"}},
                ]
            },
        )

    adapter = flipkart_adapter(app, transport=_mock(handler))
    result = await adapter.create_listing(sent)
    assert result.status == "failed"
    assert result.error is not None and result.error.error_code == "PUBLISH_FAILED"


async def test_flipkart_get_listing_404_reply() -> None:
    """GET answering 404 (unknown SKU on a real API) must not crash; it is a typed outcome."""
    from app.adapters.base import ListingNotFound

    app = build_fake()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "LISTING_NOT_FOUND"}]})

    adapter = flipkart_adapter(app, transport=_mock(handler))
    with pytest.raises((ListingNotFound, MarketplaceUnavailable)):
        await adapter.get_listing("NOPE-1")


async def test_flipkart_gzip_bomb_capped() -> None:
    """A small gzip body that inflates past the 1 MiB cap is refused (decoded size counts)."""
    import gzip

    app = build_fake()
    inflated = b"{" + b" " * (3 * 1024 * 1024) + b"}"
    packed = gzip.compress(inflated)
    assert len(packed) < 64 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=packed, headers={"content-encoding": "gzip"})

    client = flipkart_client(app, transport=_mock(handler))
    with pytest.raises(MarketplaceAmbiguous):
        await client.create([LISTING])


async def test_flipkart_request_body_has_no_commercial_keys_over_wire() -> None:
    """The bytes on the wire for a family create hold no price/MRP/stock/HSN/package key."""
    from app.adapters.flipkart.constants import FORBIDDEN_ATTRIBUTES

    app = build_fake()
    bodies: list[dict[str, Any]] = []
    inner = httpx.ASGITransport(app)

    class Spy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                bodies.append(json.loads(await request.aread()))
            return await inner.handle_async_request(request)

    adapter = flipkart_adapter(app, transport=Spy())
    result = await adapter.create_listing(fk_family())
    assert result.status == "published"
    assert bodies
    for body in bodies:
        for entry in body["listings"]:
            keys = {k.casefold() for k in entry["attributes"]}
            assert not keys & FORBIDDEN_ATTRIBUTES
            assert entry["sku"] != entry["attributes"]["style_code"][0]["value"]
