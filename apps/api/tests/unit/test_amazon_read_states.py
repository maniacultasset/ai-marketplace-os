"""S21 T18 (CP6, CP15; AC10): the states `AmazonAdapter.get_listing` reads off the fake SP-API's
GET, and the fake's per-SKU GET control and prefix reset."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.adapters.amazon_support import amazon_adapter
from tests.support.fake_marketplace import build_fake, reset_prefix

from .test_amazon_adapter import _five


@pytest.mark.parametrize(
    ("mode", "state", "status"),
    [
        ("buyable", "live", "published"),
        ("discoverable", "processing", "published"),
        ("inactive", "inactive", "published"),
        ("rejected", "rejected", "failed"),
    ],
)
async def test_amazon_get_states(mode: str, state: str, status: str) -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_get[sent.seller_sku] = mode
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    assert (got.marketplace_state, got.status) == (state, status)


async def test_amazon_get_gone_raises_not_found() -> None:
    from app.adapters.base import ListingNotFound

    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_get[sent.seller_sku] = "gone"
    with pytest.raises(ListingNotFound):
        await adapter.get_listing(sent.seller_sku)


async def test_fake_get_control_and_reset_prefix() -> None:
    app = build_fake()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as client:
        response = await client.post(
            "/__control", json={"spapi_get": {"E2E-A": "buyable", "X": "bad"}}
        )
        assert response.status_code == 200
        assert app.state.fake.spapi_get == {"E2E-A": "buyable"}
        app.state.fake.spapi_items[("S", "E2E-A")] = {
            "productType": "x",
            "attributes": {},
            "issues": [],
        }
        app.state.fake.spapi_items[("S", "OTHER")] = {
            "productType": "x",
            "attributes": {},
            "issues": [],
        }
        response = await client.post("/__control/reset", json={"prefix": "E2E-"})
        assert response.json() == {"reset": 1}
    assert list(app.state.fake.spapi_items) == [("S", "OTHER")]
    assert app.state.fake.spapi_get == {}
    assert reset_prefix(app.state.fake, "") == 0


# ---- SI4 A16 (AC13): the read's issues come back as field errors ----


def _row(code: str = "90220") -> Any:
    from app.adapters.error_map import ErrorMapRow
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorClass, RetryClass, Severity

    return ErrorMapRow(
        marketplace="amazon",
        match_on="code",
        raw_value=code,
        version=1,
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        error_class=ErrorClass.INVALID_VALUE,
        retry_class=RetryClass.USER_ACTION,
        severity=Severity.ERROR,
        auto_fixable=False,
        fix_action=None,
        field_hint=None,
        plain_text="{field} is not accepted on {marketplace}.",
        seller_action="Change the value.",
    )


async def _read(issues: list[Any], rows: Any = ()) -> Any:
    from tests.adapters.amazon_support import SELLER

    app = build_fake()
    adapter = amazon_adapter(app, rows=rows)
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["issues"] = issues
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    return got


def _field(got: Any, path: str) -> Any:
    (one,) = [f for f in got.fields if f.field == path]
    return one


async def test_amazon_read_error_on_item_name_is_a_failed_title() -> None:
    issue = {
        "code": "90220",
        "message": "Bad title.",
        "severity": "ERROR",
        "attributeNames": ["item_name"],
    }
    got = await _read([issue], rows=[_row()])
    title = _field(got, "content.title")
    assert title.status == "failed"
    error = title.error
    assert (error.error_code, error.source.value, error.marketplace.value) == (
        "VALUE_NOT_ALLOWED",
        "amazon_adapter",
        "amazon",
    )
    assert (got.status, got.marketplace_state) == ("partially_published", "partially_live")


async def test_amazon_read_unmapped_code_is_unknown() -> None:
    issue = {
        "code": "77777",
        "message": "Odd.",
        "severity": "ERROR",
        "attributeNames": ["item_name"],
    }
    error = _field(await _read([issue]), "content.title").error
    assert (error.error_code, error.error_class.value) == ("MARKETPLACE_UNKNOWN_ERROR", "unknown")


async def test_amazon_read_warning_is_a_warning() -> None:
    issue = {
        "code": "90220",
        "message": "Hmm.",
        "severity": "WARNING",
        "attributeNames": ["item_name"],
    }
    title = _field(await _read([issue], rows=[_row()]), "content.title")
    assert title.status == "succeeded"
    assert title.error.severity.value == "warning"


async def test_amazon_read_scrubs_tokens() -> None:
    lwa = "Atza|" + "x" * 40
    issue = {
        "code": "77777",
        "message": f"bad token {lwa}",
        "severity": "ERROR",
        "attributeNames": ["item_name"],
    }
    error = _field(await _read([issue]), "content.title").error
    assert lwa not in error.message


async def test_amazon_read_redacts_the_live_session_token() -> None:
    from tests.adapters.amazon_support import SELLER

    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    [live] = adapter._client.used_secrets()
    shaped = f"tok:{live}:end"
    app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["issues"] = [
        {"code": "77777", "message": shaped, "severity": "ERROR", "attributeNames": ["item_name"]}
    ]
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    error = _field(got, "content.title").error
    assert live not in error.message
    assert error.source.value == "amazon_adapter"


async def test_amazon_read_item_level_error_is_a_listing_field() -> None:
    issue = {"code": "90220", "message": "Refused.", "severity": "ERROR"}
    got = await _read([issue], rows=[_row()])
    listing = _field(got, "listing")
    assert listing.status == "failed"
    assert (got.status, got.marketplace_state) == ("failed", "rejected")


async def test_amazon_read_clean_has_no_errors() -> None:
    got = await _read([])
    assert all(f.error is None and f.status == "succeeded" for f in got.fields)
