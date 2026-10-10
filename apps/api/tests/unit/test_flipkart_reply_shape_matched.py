"""S22 re-check (F1): the reply-shape property with rows that answer the SKUs actually sent.

`test_flipkart_reply_shape.py`'s property draws row SKUs from P-1/P-2/OTHER, which never match the
family's seller SKUs (P-S-NAVYBLUE, P-M-NAVYBLUE), so the per-SKU paths (`_issue_items`,
`_outcome`, `_not_found`, `get_listing`'s matched entry) only ever see "no answer". Here every
row names a real SKU and carries drifted `errors`, `status`, `attributes` and `listing_id`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
from app.adapters.base import AdapterError, ListingNotFound
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.publish import ListingResult
from tests.adapters.flipkart_support import fk_family, flipkart_adapter
from tests.support.fake_marketplace import build_fake

SKUS = ["P-M-NAVYBLUE", "P-S-NAVYBLUE"]

_scalar = st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8)
_json = st.recursive(
    _scalar,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=10,
)
_issue = st.fixed_dictionaries(
    {},
    optional={
        "code": _json | st.sampled_from(["NOT_FOUND", "LISTING_NOT_FOUND", "INVALID_VALUE"]),
        "description": _json,
        "severity": st.sampled_from(["ERROR", "WARNING", "INFO"]) | _json,
        "attribute": st.sampled_from(["title", "images", "colour", "size"]) | _json,
        "category": _json,
    },
)
_row = st.fixed_dictionaries(
    {"sku": st.sampled_from(SKUS)},
    optional={
        "status": st.sampled_from(["SUCCESS", "FAILURE", "ACTIVE", "INACTIVE"]) | _json,
        "listing_id": _json,
        "errors": st.lists(_issue | _json, max_size=3) | _json,
        "attributes": st.dictionaries(
            st.sampled_from(["title", "images", "colour", "size", "zz"]), _json, max_size=3
        )
        | _json,
    },
)
_body = st.fixed_dictionaries(
    {},
    optional={
        "results": st.lists(_row, max_size=3),
        "listings": st.lists(_row, max_size=3),
        "errors": st.lists(_issue | _json, max_size=2) | _json,
    },
)
_status = st.sampled_from([200, 207, 400, 404, 422])


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=_body, status=_status)
def test_flipkart_matched_rows_any_shape_typed_outcome(body: Any, status: int) -> None:
    app = build_fake()
    raw = json.dumps(body).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=raw)

    sent = fk_family(
        children=[{"size": "S", "color": "navy blue"}, {"size": "M", "color": "navy blue"}],
        style="P",
    )
    assert sorted(i.seller_sku for i in sent.items) == SKUS

    async def run() -> None:
        adapter = flipkart_adapter(app, transport=httpx.MockTransport(handler))
        with contextlib.suppress(AdapterError):
            assert isinstance(await adapter.create_listing(sent), ListingResult)
        for sku in SKUS:
            with contextlib.suppress(AdapterError, ListingNotFound):
                out = await adapter.update_listing(
                    sent, fields={sku: ["content.title", "images[0]"]}
                )
                assert isinstance(out, ListingResult)
            with contextlib.suppress(AdapterError, ListingNotFound):
                assert isinstance(await adapter.get_listing(sku), ListingResult)

    asyncio.run(run())
