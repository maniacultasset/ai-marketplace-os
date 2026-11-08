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
    ("mode", "state", "status", "not_buyable"),
    [
        ("buyable", "live", "published", False),
        # SI9 B3 (CP-D6): a summary with an ASIN, no BUYABLE and no ERROR is on Amazon, not buyable
        ("discoverable", "inactive", "published", True),
        ("inactive", "inactive", "published", True),
        ("no_asin", "inactive", "published", False),
        ("no_summary", "processing", "published", False),
        ("rejected", "rejected", "failed", False),
    ],
)
async def test_amazon_get_states(mode: str, state: str, status: str, not_buyable: bool) -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_get[sent.seller_sku] = mode
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    assert (got.marketplace_state, got.status, got.not_buyable) == (state, status, not_buyable)
    if mode in ("discoverable", "inactive"):
        assert got.external_id and got.external_id.startswith("B0")
    if mode in ("no_asin", "no_summary"):
        assert got.external_id is None


async def test_amazon_discoverable_with_attribute_error_is_partial_not_unbuyable() -> None:
    issue = {
        "code": "90220",
        "message": "Bad title.",
        "severity": "ERROR",
        "attributeNames": ["item_name"],
    }
    got = await _read([issue])
    assert (got.marketplace_state, got.not_buyable) == ("partially_live", False)


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
            "/__control",
            json={
                "spapi_get": {
                    "E2E-A": "buyable",
                    "E2E-B": "no_summary",
                    "E2E-C": "no_asin",
                    "X": "bad",
                }
            },
        )
        assert response.status_code == 200
        assert app.state.fake.spapi_get == {
            "E2E-A": "buyable",
            "E2E-B": "no_summary",
            "E2E-C": "no_asin",
        }
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


# ---- SI4 round 3 F2 (AC13): every ERROR issue of a read is its own item ----


def _read_items(got: Any) -> list[tuple[str, str | None, str]]:
    return [(e.field, e.raw_code, e.severity.value) for e in got.errors]


async def test_amazon_read_two_errors_on_one_attribute_are_two_items() -> None:
    issues = [
        {
            "code": "90220",
            "message": "Bad title.",
            "severity": "ERROR",
            "attributeNames": ["item_name"],
        },
        {
            "code": "77777",
            "message": "Also odd.",
            "severity": "ERROR",
            "attributeNames": ["item_name"],
        },
    ]
    got = await _read(issues, rows=[_row()])
    assert _read_items(got) == [
        ("content.title", "90220", "error"),
        ("content.title", "77777", "error"),
    ]
    assert _field(got, "content.title").status == "failed"  # the per-path fold is unchanged


async def test_amazon_read_two_item_level_errors_are_two_items() -> None:
    issues = [
        {"code": "90220", "message": "Refused.", "severity": "ERROR"},
        {"code": "77777", "message": "Also refused.", "severity": "ERROR"},
    ]
    got = await _read(issues, rows=[_row()])
    assert _read_items(got) == [("listing", "90220", "error"), ("listing", "77777", "error")]
    assert (got.status, got.marketplace_state) == ("failed", "rejected")


async def test_amazon_read_keeps_a_warning_beside_an_error_in_read_order() -> None:
    issues = [
        {
            "code": "77777",
            "message": "Hmm.",
            "severity": "WARNING",
            "attributeNames": ["item_name"],
        },
        {"code": "90220", "message": "Bad.", "severity": "ERROR", "attributeNames": ["item_name"]},
    ]
    got = await _read(issues, rows=[_row()])
    assert _read_items(got) == [
        ("content.title", "77777", "warning"),
        ("content.title", "90220", "error"),
    ]


async def test_status_sync_takes_every_read_error_from_a_real_read() -> None:
    """The sync's items come from the adapter's own read (the real fold, no fake read)."""
    from app.workers.status_sync import _read_errors

    issues = [
        {
            "code": "90220",
            "message": "Bad title.",
            "severity": "ERROR",
            "attributeNames": ["item_name"],
        },
        {
            "code": "77777",
            "message": "Also odd.",
            "severity": "ERROR",
            "attributeNames": ["item_name"],
        },
        {"code": "90220", "message": "Refused.", "severity": "ERROR"},
        {"code": "77777", "message": "Also refused.", "severity": "ERROR"},
    ]
    got = await _read(issues, rows=[_row()])
    assert sorted((e.field, e.raw_code) for e in _read_errors(got)) == [
        ("content.title", "77777"),
        ("content.title", "90220"),
        ("listing", "77777"),
        ("listing", "90220"),
    ]


# ---- SI8 fix round 1 (R1-1; CP-D4): a colour child's image slots on a read ----


def _image_issue(name: str) -> dict[str, Any]:
    return {"code": "90220", "message": "Bad image.", "severity": "ERROR", "attributeNames": [name]}


async def _read_child(issues: list[Any]) -> tuple[Any, dict[str, int]]:
    """The AC5 family (S, Z, R1, B1, R2) published to the fake; the Red child read back."""
    from tests.adapters.amazon_support import SELLER, colour_family

    app = build_fake()
    adapter = amazon_adapter(app, rows=[_row()])
    sent, at = colour_family()
    await adapter.create_listing(sent)
    (red,) = [
        u
        for u in sent.units()
        if u.item_role == "child" and u.variant_attributes == {"size": "M", "color": "red"}
    ]
    app.state.fake.spapi_items[(SELLER, red.seller_sku)]["issues"] = issues
    return await adapter.get_listing(red.seller_sku, listing_id=sent.listing_id), at


async def test_amazon_read_child_image_issue_never_lands_on_the_wrong_image() -> None:
    """Red sends main=R1, other_1=R2, other_2=Z, so `other_product_image_locator_1` is R2, not
    images[1] (Z). A read has no version to rebuild the slots from: the issue fails `listing`,
    and no image path of the child reads as succeeded (a retry of Z would never fix R2)."""
    got, at = await _read_child([_image_issue("other_product_image_locator_1")])
    images = {f.field: f.status for f in got.fields if f.field.startswith("images[")}
    assert images == {}, images
    assert [e.field for e in got.errors] == ["listing"]
    assert at["Z"] == 1 and at["R2"] == 4  # the finding's layout: the old index said images[1]
    assert got.status == "partially_published"


async def test_amazon_read_child_text_issue_keeps_its_path() -> None:
    got, _ = await _read_child([_image_issue("item_name")])
    assert _field(got, "content.title").status == "failed"
    assert [e.field for e in got.errors] == ["content.title"]


async def test_amazon_read_standalone_image_issue_keeps_its_image_path() -> None:
    """A standalone sends every image in version order, so slot k is images[k] (unchanged)."""
    got = await _read([_image_issue("other_product_image_locator_1")], rows=[_row()])
    assert _field(got, "images[1]").status == "failed"
    assert [e.field for e in got.errors] == ["images[1]"]
