"""Audit JOBS-3: a 200 SP-API reply that is valid JSON of an unexpected shape gives a typed
outcome (a `ListingResult`, or `MarketplaceAmbiguous` after a write / `MarketplaceUnavailable`
on a GET), never a raw TypeError/KeyError. The audit repros with real assertions, plus the
reply-shape property ported from test_flipkart_reply_shape.py."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest
from app.adapters.base import (
    AdapterError,
    ListingNotFound,
    MarketplaceAmbiguous,
    MarketplaceUnavailable,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.publish import ListingResult
from tests.adapters.amazon_support import amazon_adapter, amazon_family, amazon_item
from tests.support.fake_marketplace import build_fake


def _reply(body: Any, status: int = 200) -> httpx.MockTransport:
    raw = json.dumps(body).encode()
    return httpx.MockTransport(lambda request: httpx.Response(status, content=raw))


@pytest.mark.parametrize("key", ["attributeNames", "categories"])
@pytest.mark.parametrize("bad", [7, "item_name", {"a": 1}, True])
async def test_create_issue_names_not_a_list_name_no_attribute(key: str, bad: Any) -> None:
    """An accepted PUT whose WARNING issue carries a scalar `attributeNames`/`categories` is
    still an accepted write: the issue just names no attribute."""
    issue = {"code": "W1", "message": "note", "severity": "WARNING", key: bad}
    body = {"status": "ACCEPTED", "submissionId": "s1", "issues": [issue]}
    out = await amazon_adapter(build_fake(), transport=_reply(body)).create_listing(amazon_item())
    assert isinstance(out, ListingResult)
    assert out.status == "published"


async def test_create_error_issue_names_not_a_list_fails_the_item() -> None:
    issue = {"code": "E1", "message": "bad", "severity": "ERROR", "attributeNames": 7}
    body = {"status": "INVALID", "submissionId": "s1", "issues": [issue]}
    out = await amazon_adapter(build_fake(), transport=_reply(body)).create_listing(amazon_item())
    assert isinstance(out, ListingResult)
    assert out.status == "failed"


@pytest.mark.parametrize(
    "body",
    [
        [{"status": "ACCEPTED"}],
        "ACCEPTED",
        {"status": 1, "submissionId": "s1"},
        {"status": "ACCEPTED", "issues": 3},
        {"status": "ACCEPTED", "issues": {"code": "X"}},
        {"status": "ACCEPTED", "errors": "E"},
    ],
    ids=["list", "string", "status-int", "issues-int", "issues-obj", "errors-str"],
)
async def test_write_reply_unreadable_is_ambiguous(body: Any) -> None:
    """The PUT/PATCH was sent and may be applied: the caller must read before any resend."""
    adapter = amazon_adapter(build_fake(), transport=_reply(body))
    with pytest.raises(MarketplaceAmbiguous) as raised:
        await adapter.create_listing(amazon_item())
    assert raised.value.report.error_code == ErrorCode.MARKETPLACE_UNCONFIRMED
    adapter = amazon_adapter(build_fake(), transport=_reply(body))
    with pytest.raises(MarketplaceAmbiguous):
        await adapter.update_listing(amazon_item(), fields=["content.title"])


@pytest.mark.parametrize(
    "body",
    [
        {"summaries": {"asin": "B0"}},
        {"summaries": [{"status": 1}]},
        {"summaries": [{"status": ["BUYABLE", 2]}]},
        {"summaries": ["B0"]},
        {"attributes": [{"item_name": "x"}]},
        {"issues": 3},
        ["B0"],
    ],
    ids=[
        "summaries-obj",
        "status-int",
        "status-mixed",
        "summary-str",
        "attributes-list",
        "issues-int",
        "list",
    ],
)
async def test_get_reply_unreadable_is_unavailable(body: Any) -> None:
    adapter = amazon_adapter(build_fake(), transport=_reply(body))
    with pytest.raises(MarketplaceUnavailable):
        await adapter.get_listing("SKU-1")


async def test_get_error_issue_names_not_a_list_is_item_level() -> None:
    issue = {"code": "E1", "severity": "ERROR", "attributeNames": 7}
    body = {"summaries": [{"asin": "B0", "status": []}], "issues": [issue]}
    out = await amazon_adapter(build_fake(), transport=_reply(body)).get_listing("SKU-1")
    assert out.status == "failed"


# ---- the property: any reply shape gives a typed outcome ----

_scalar = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
_json = st.recursive(
    _scalar,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=12,
)
_issue = st.fixed_dictionaries(
    {},
    optional={
        "code": _scalar,
        "message": _scalar,
        "severity": st.sampled_from(["ERROR", "WARNING", "INFO"]) | _scalar,
        "attributeNames": st.lists(st.sampled_from(["item_name", "color", "zz"]), max_size=2)
        | _json,
        "categories": _json,
    },
)
_body = (
    st.fixed_dictionaries(
        {},
        optional={
            "sku": _scalar,
            "status": st.sampled_from(["ACCEPTED", "INVALID"]) | _scalar,
            "submissionId": _scalar | _json,
            "issues": st.lists(_issue | _json, max_size=3) | _json,
            "errors": _json,
            "summaries": st.lists(
                st.fixed_dictionaries(
                    {},
                    optional={
                        "asin": _scalar,
                        "status": st.lists(st.sampled_from(["BUYABLE", "DISCOVERABLE"])) | _json,
                    },
                )
                | _json,
                max_size=2,
            )
            | _json,
            "attributes": st.dictionaries(st.sampled_from(["item_name", "color"]), _json) | _json,
        },
    )
    | _json
)
_status = st.sampled_from([200, 400, 404, 409, 422])


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(body=_body, status=_status)
def test_amazon_any_reply_typed_outcome(body: Any, status: int) -> None:
    """Whatever JSON SP-API answers, create, update and get give a `ListingResult` or a typed
    adapter outcome, never a TypeError/KeyError/ValidationError."""
    app = build_fake()
    sent = amazon_family()
    sku = sent.items[0].seller_sku

    async def run() -> None:
        adapter = amazon_adapter(app, transport=_reply(body, status))
        with contextlib.suppress(AdapterError):
            assert isinstance(await adapter.create_listing(sent), ListingResult)
        with contextlib.suppress(AdapterError, ListingNotFound):
            out = await adapter.update_listing(sent, fields={sku: ["content.title"]})
            assert isinstance(out, ListingResult)
        with contextlib.suppress(AdapterError, ListingNotFound):
            assert isinstance(await adapter.get_listing(sku), ListingResult)

    asyncio.run(run())
