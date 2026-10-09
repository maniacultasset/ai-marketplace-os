"""S22 fix round 2 (F1): a Flipkart reply whose `errors` is not a list (a bool, a number, a
string) gives a typed outcome, never a raw TypeError. These are the tester's repros, with real
assertions, plus the reply-shape property that found them."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from app.adapters.base import AdapterError, ListingNotFound
from app.adapters.flipkart import client as fk_client
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.publish import ListingResult
from tests.adapters.flipkart_support import fk_family, fk_item, flipkart_adapter, flipkart_client
from tests.support.fake_marketplace import build_fake

LISTING = {"sku": "SKU-1", "vertical": "kurta", "attributes": {"title": [{"value": "K"}]}}
BAD = [True, 5, "E"]


def _mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("bad", BAD)
async def test_create_2xx_row_errors_not_a_list_after_write(bad: Any) -> None:
    """The per-call log line reads the row errors after the write was sent; a scalar there
    must not replace the reply with a TypeError (S20 C2)."""
    app = build_fake()

    def handler(request: httpx.Request) -> httpx.Response:
        row = {"sku": "SKU-1", "status": "SUCCESS", "errors": bad}
        return httpx.Response(200, json={"results": [row]})

    response = await flipkart_client(app, transport=_mock(handler)).create([LISTING])
    assert response.status == 200
    assert fk_client._first_code(response.json) is None


@pytest.mark.parametrize("bad", BAD)
async def test_update_row_errors_not_a_list(bad: Any) -> None:
    app = build_fake()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/update"):
            row = {"sku": "U-1", "status": "FAILURE", "errors": bad}
            return httpx.Response(200, json={"results": [row], "errors": [{"code": "E"}]})
        rows = [{"sku": e["sku"], "status": "SUCCESS"} for e in body.get("listings", [])]
        return httpx.Response(200, json={"results": rows})

    adapter = flipkart_adapter(app, transport=_mock(handler))
    out = await adapter.update_listing(fk_item(sku="U-1"), fields={"U-1": ["content.title"]})
    assert isinstance(out, ListingResult)
    assert out.status != "published"


@pytest.mark.parametrize("bad", BAD)
async def test_update_whole_request_errors_not_a_list(bad: Any) -> None:
    """A non-2xx reply is every SKU's answer (`_answers`); its scalar `errors` must not reach
    `_not_found` as an iterable."""
    app = build_fake()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": bad})

    adapter = flipkart_adapter(app, transport=_mock(handler))
    out = await adapter.update_listing(fk_item(sku="U-1"), fields={"U-1": ["content.title"]})
    assert isinstance(out, ListingResult)
    assert out.status == "failed"


@pytest.mark.parametrize("bad", BAD)
async def test_get_entry_errors_not_a_list(bad: Any) -> None:
    app = build_fake()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"listings": [{"sku": "G-1", "status": "ACTIVE", "errors": bad}]}
        )

    out = await flipkart_adapter(app, transport=_mock(handler)).get_listing("G-1")
    assert isinstance(out, ListingResult)
    assert out.status == "published"


def test_log_code_never_replaces_the_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """The log-line code helper is best-effort: if it ever raises, the call's typed outcome
    still stands (the failure is logged, not swallowed silently)."""

    def boom(body: Any) -> str | None:
        raise RuntimeError("drifted shape")

    monkeypatch.setattr(fk_client, "_first_code", boom)
    assert fk_client._log_code({"errors": [{"code": "X"}]}) is None


# ---- the tester's property that found F1: any reply shape gives a typed outcome ----

_scalar = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
_json = st.recursive(
    _scalar,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=12,
)
_sku_row = st.fixed_dictionaries(
    {},
    optional={
        "sku": st.sampled_from(["P-1", "P-2", "OTHER"]) | _scalar,
        "status": st.sampled_from(["SUCCESS", "FAILURE", "ACTIVE", "x"]) | _scalar,
        "listing_id": _scalar | st.text(min_size=60, max_size=80),
        "errors": st.lists(
            st.fixed_dictionaries(
                {},
                optional={
                    "code": _scalar,
                    "description": _scalar,
                    "severity": st.sampled_from(["ERROR", "WARNING", "INFO"]) | _scalar,
                    "attribute": st.sampled_from(["title", "images", "colour", "zz"]) | _scalar,
                    "category": _scalar,
                },
            )
            | _json,
            max_size=3,
        )
        | _json,
        "attributes": _json,
    },
)
_body = (
    st.fixed_dictionaries(
        {},
        optional={
            "request_id": _scalar | _json,
            "results": st.lists(_sku_row | _json, max_size=4) | _json,
            "listings": st.lists(_sku_row | _json, max_size=4) | _json,
            "errors": _json,
        },
    )
    | _json
)
_status = st.sampled_from([200, 201, 207, 400, 404, 409, 422])


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=_body, status=_status)
def test_flipkart_any_reply_typed_outcome(body: Any, status: int) -> None:
    """Whatever JSON Flipkart answers (shape drift before DW1), create, update and get give a
    `ListingResult` or a typed adapter outcome, never a TypeError/KeyError/ValidationError."""
    app = build_fake()
    raw = json.dumps(body).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=raw)

    sent = fk_family(
        children=[{"size": "S", "color": "navy blue"}, {"size": "M", "color": "navy blue"}],
        style="P",
    )

    async def run() -> None:
        adapter = flipkart_adapter(app, transport=_mock(handler))
        try:
            out = await adapter.create_listing(sent)
            assert isinstance(out, ListingResult)
        except AdapterError:
            pass
        sku = sent.items[0].seller_sku
        try:
            out = await adapter.update_listing(sent, fields={sku: ["content.title"]})
            assert isinstance(out, ListingResult)
        except (AdapterError, ListingNotFound):
            pass
        try:
            out = await adapter.get_listing(sku)
            assert isinstance(out, ListingResult)
        except (AdapterError, ListingNotFound):
            pass

    asyncio.run(run())
