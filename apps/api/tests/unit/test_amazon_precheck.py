"""SI13 T4 (P:A3, D14; AC5, AC6; CP-D13): the pure catalog and restriction pre-check mapping, and
`AmazonAdapter.precheck_catalog` over the in-process fake (chunking, restriction lookups, failures
that never raise, scrubbing)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import httpx
import pytest
from app.adapters.amazon.precheck import (
    allowed_link,
    brand_relation,
    from_catalog,
    from_restrictions,
    link_in,
)
from app.adapters.base import AdapterItem, AdapterListing
from mkos_schemas.enums import Severity
from tests.adapters.amazon_support import (
    SELLER_FACTS,
    amazon_adapter,
    amazon_family,
    amazon_item,
)
from tests.support.fake_marketplace import build_fake, catalog_asin

EAN = "4006381333931"  # SELLER_FACTS' barcode
BRAND = "Test Brand"


def _entry(ean: str, brand: str | None, asin: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"marketplaceId": "A21TJRUUN4KGV"}
    if brand is not None:
        summary["brand"] = brand
    return {
        "asin": asin or catalog_asin(ean),
        "summaries": [summary],
        "identifiers": [{"identifiers": [{"identifierType": "EAN", "identifier": ean}]}],
    }


def _facts(**over: Any) -> Any:
    return dataclasses.replace(SELLER_FACTS, **over)


def _with(sent: AdapterListing, **over: Any) -> AdapterListing:
    return dataclasses.replace(
        sent, unit_facts={u.product_id: _facts(**over) for u in sent.units()}
    )


# ---- brand relation (P:D14) ----


@pytest.mark.parametrize(
    ("ours", "theirs", "want"),
    [
        ("Zebra", "ZEBRA", "same"),
        ("Zebra", " zebra ", "same"),
        ("Zebra", "Zebra Co", "similar"),
        ("Zebra", "Zebra Private Limited", "similar"),
        ("Zebra Private Limited", "Zebra", "similar"),
        ("Zebra", "Zébra", "other"),
        ("Acme", "Zebra Co", "other"),
        ("Zebra", "Zeb", "other"),  # whole tokens, never characters
        ("", "Zebra", "other"),
        ("Ｚｅｂｒａ", "Zebra", "same"),  # NFKC fold  # noqa: RUF001
        # form-only differences are never the blocking `other` (round 1 fix)
        ("Zebra", "ZEBRA\u2122", "same"),  # NFKC would turn the mark into "TM"
        ("Zebra", "Zebra \u2122", "same"),
        ("Zebra", "Zebra\u00ae", "same"),
        ("Haldiram's", "Haldirams", "same"),  # the apostrophe never splits a token
        ("Haldiram\u2019s", "Haldirams", "same"),
        ("Mamaearth", "Mama Earth", "similar"),  # spacing only
        # the apostrophe is also read as a separator (round 2 fix)
        ("Haldiram's", "Haldiram", "similar"),
        ("Haldiram", "Haldiram\u2019s", "similar"),
        ("L'Oreal Paris", "L Oreal", "similar"),
        ("L'Oreal", "LOreal", "same"),
        ("Haldiram's", "Bikaji's", "other"),
        ("Zeb", "Zebra", "other"),
    ],
)
def test_brand_relation(ours: str, theirs: str, want: str) -> None:
    assert brand_relation(ours, theirs) == want


# ---- from_catalog ----


def test_other_brand_is_an_error_at_the_identifier() -> None:
    sent = amazon_item()
    items, matched, others = from_catalog(sent, sent.units(), [_entry(EAN, "Acme Traders")])
    (item,) = items
    assert item.severity is Severity.ERROR and item.error_code == "IDENTIFIER_DUPLICATE"
    assert item.field == "facts.identifier" and item.raw_code == "catalog:other_brand"
    assert item.message.startswith("This barcode belongs to another brand on Amazon.")
    assert "Acme" not in item.message and EAN not in item.message
    assert matched == [] and others == [catalog_asin(EAN)]


@pytest.mark.parametrize(
    ("theirs", "code"),
    [
        (BRAND, "catalog:same_brand"),
        ("Test Brand Co", "catalog:similar_brand"),
        (None, "catalog:no_brand"),
    ],
)
def test_warning_cases(theirs: str | None, code: str) -> None:
    sent = amazon_item()
    items, matched, _ = from_catalog(sent, sent.units(), [_entry(EAN, theirs)])
    (item,) = items
    assert item.severity is Severity.WARNING and item.raw_code == code
    assert matched == (
        [(sent.seller_sku, catalog_asin(EAN))] if code.endswith("same_brand") else []
    )


def test_a_child_error_sits_at_its_identifier_fact() -> None:
    from app.services.auto_fix.policy import fact_key

    sent = amazon_family()
    child = sent.units()[1]
    facts = {
        u.product_id: _facts(identifier_value=f"40063813339{n:02d}")
        for n, u in enumerate(sent.units())
    }
    sent = dataclasses.replace(sent, unit_facts=facts)
    ean = facts[child.product_id].identifier_value
    items, _, _ = from_catalog(sent, sent.units(), [_entry(str(ean), "Acme Traders")])
    (item,) = items
    assert item.field == f"variants[{child.seller_sku}].facts.identifier"
    assert fact_key(item.field) == "identifier"


def test_no_hit_and_garbage_give_nothing() -> None:
    sent = amazon_item()
    for reply in ([], None, "x", [{"asin": 3}], [{"asin": "B0/ABCDEF1", "identifiers": []}]):
        assert from_catalog(sent, sent.units(), reply) == ([], [], [])


# ---- from_restrictions ----


def _reply(code: str, link: str = "https://sellercentral.amazon.in/hz/approval?asin=B0X") -> Any:
    return {
        "restrictions": [
            {
                "reasons": [
                    {
                        "reasonCode": code,
                        "message": "You need approval.\x07",
                        "links": [{"resource": link}],
                    }
                ]
            }
        ]
    }


@pytest.mark.parametrize("code", ["APPROVAL_REQUIRED", "NOT_ELIGIBLE"])
def test_restriction_is_an_error_with_the_link(code: str) -> None:
    (item,) = from_restrictions(_reply(code))
    assert item.severity is Severity.ERROR and item.error_code == "ACCOUNT_PERMISSION_DENIED"
    assert item.field == "listing" and item.raw_code == f"restriction:{code}"
    assert "\x07" not in item.message
    assert link_in(item.message) == "https://sellercentral.amazon.in/hz/approval?asin=B0X"


def test_unknown_reason_gives_nothing() -> None:
    assert from_restrictions(_reply("SOMETHING_ELSE")) == []
    assert from_restrictions({"restrictions": []}) == []
    assert from_restrictions("garbage") == []


def test_bad_link_is_dropped_and_secrets_scrubbed() -> None:
    reply = _reply("APPROVAL_REQUIRED", "http://sellercentral.amazon.in/x")
    reply["restrictions"][0]["reasons"][0]["message"] = "token Atza|SECRETVALUE1234 echoed"
    (item,) = from_restrictions(reply, secrets=["Atza|SECRETVALUE1234"])
    assert link_in(item.message) is None and "SECRETVALUE1234" not in item.message


# ---- allowed_link (CP-D13) ----


@pytest.mark.parametrize(
    "url",
    [
        "https://sellercentral.amazon.in/x",
        "https://sellercentral.amazon.com/hz/approvalrequest?asin=B0ABCDEF12",
    ],
)
def test_allowed_link_accepts(url: str) -> None:
    assert allowed_link(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://sellercentral.amazon.in/x",
        "https://evil.example/",
        "https://user@sellercentral.amazon.in/",
        "https://sellercentral.amazon.in:8443/",
        "https://sellercentral.amazon.in/" + "a" * 2020,
        "javascript:alert(1)",
        "https://sellercentral.amazon.in.evil.io/",
        "https://SELLERCENTRAL.amazon.in/x",
        "https://sellercentral.amazon.in/x y",
        None,
        3,
    ],
)
def test_allowed_link_refuses(url: Any) -> None:
    assert allowed_link(url) is None


# ---- the adapter method over the fake ----


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _paths(app: Any) -> list[str]:
    return [c["path"] for c in app.state.fake.calls]


async def test_other_brand_and_restriction_end_to_end() -> None:
    app = build_fake()
    await _control(
        app,
        spapi_catalog={
            EAN: {"scenario": "same_brand_ean", "brand": BRAND, "restriction": "brand_restricted"}
        },
    )
    sent = amazon_item()
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=True)
    codes = sorted(i.raw_code or "" for i in result.items)
    assert codes == ["catalog:same_brand", "restriction:APPROVAL_REQUIRED"]
    assert result.matched == ((sent.seller_sku, catalog_asin(EAN)),) and result.failed == ()
    assert _paths(app) == ["/catalog/2022-04-01/items", "/listings/2021-08-01/restrictions"]


async def test_restrictions_off_makes_no_restriction_call() -> None:
    app = build_fake()
    await _control(
        app, spapi_catalog={EAN: {"scenario": "other_brand_ean", "restriction": "brand_restricted"}}
    )
    result = await amazon_adapter(app).precheck_catalog(amazon_item(), restrictions=False)
    assert [i.raw_code for i in result.items] == ["catalog:other_brand"]
    assert _paths(app) == ["/catalog/2022-04-01/items"]


async def test_21_children_two_calls_and_exempt_skipped() -> None:
    app = build_fake()
    rows = [{"size": f"S{n}", "color": "red"} for n in range(22)]
    sent = amazon_family(children=rows)
    facts = {}
    for n, unit in enumerate(sent.units()):
        exempt = n == 1  # the first child is GTIN-exempt
        facts[unit.product_id] = _facts(
            identifier_value=None if exempt else f"8900000000{n:03d}",
            identifier_exempt=exempt,
        )
    sent = dataclasses.replace(sent, unit_facts=facts)
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=True)
    calls = [c for c in app.state.fake.calls if c["path"].startswith("/catalog")]
    assert len(calls) == 2  # 21 barcoded children: 20 + 1 (parent and the exempt child skipped)
    first = calls[0]["query"].split("identifiers=")[1].split("&")[0].split("%2C")
    assert len(first) == 20
    assert result.items == () and result.failed == ()


async def test_a_5xx_on_one_chunk_fails_its_skus_only() -> None:
    app = build_fake()
    rows = [{"size": f"S{n}", "color": "red"} for n in range(21)]
    sent = amazon_family(children=rows)
    facts = {
        u.product_id: _facts(identifier_value=f"8900000000{n:03d}")
        for n, u in enumerate(sent.units())
    }
    sent = dataclasses.replace(sent, unit_facts=facts)
    kids = sorted((u for u in sent.units() if u.item_role == "child"), key=lambda u: u.seller_sku)
    bad = str(facts[kids[0].product_id].identifier_value)
    late = str(facts[kids[-1].product_id].identifier_value)
    await _control(
        app,
        spapi_catalog={bad: {"scenario": "catalog_5xx"}, late: {"scenario": "other_brand_ean"}},
    )
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=False)
    assert set(result.failed) == {u.seller_sku for u in kids[:20]}
    assert [i.raw_code for i in result.items] == ["catalog:other_brand"]


async def test_a_restriction_5xx_fails_the_matched_sku() -> None:
    app = build_fake()
    await _control(
        app,
        spapi_catalog={
            EAN: {"scenario": "same_brand_ean", "brand": BRAND, "restriction": "restrictions_5xx"}
        },
    )
    sent = amazon_item()
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=True)
    assert result.failed == (sent.seller_sku,)
    assert [i.raw_code for i in result.items] == ["catalog:same_brand"]


async def test_an_echoed_token_is_scrubbed() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    token = adapter._client._tokens.token
    await _control(
        app,
        spapi_catalog={
            EAN: {"scenario": "same_brand_ean", "brand": BRAND, "restriction": "brand_restricted"}
        },
    )

    original = adapter._client.get_restrictions

    async def echo(asin: str) -> Any:
        response = await original(asin)
        response.json["restrictions"][0]["reasons"][0]["message"] = f"bad token {token}"
        return response

    adapter._client.get_restrictions = echo
    result = await adapter.precheck_catalog(amazon_item(), restrictions=True)
    assert all(token not in i.message for i in result.items)


async def test_unit_without_barcode_sends_nothing() -> None:
    app = build_fake()
    sent = _with(amazon_item(), identifier_value=None, identifier_exempt=True)
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=True)
    assert result.items == () and app.state.fake.calls == []


def test_parent_is_never_searched() -> None:
    from app.adapters.amazon.precheck import barcode_of

    sent = amazon_family()
    parent = sent.units()[0]
    assert parent.item_role == "parent" and barcode_of(sent, parent) is None
    odd = AdapterItem(uuid.uuid4(), "X-1", "standalone")
    assert barcode_of(sent, odd) is None  # no facts


# ---- round 1 fix: a search over more than one page ----


def _paged_family(n: int) -> tuple[Any, list[Any], dict[Any, Any]]:
    rows = [{"size": f"S{k}", "color": "red"} for k in range(n)]
    sent = amazon_family(children=rows)
    facts = {
        u.product_id: _facts(identifier_value=f"8900000000{k:03d}")
        for k, u in enumerate(sent.units())
    }
    sent = dataclasses.replace(sent, unit_facts=facts)
    kids = sorted((u for u in sent.units() if u.item_role == "child"), key=lambda u: u.seller_sku)
    return sent, kids, facts


async def test_a_match_on_page_two_is_flagged() -> None:
    app = build_fake()
    sent, kids, facts = _paged_family(15)
    spec = {"scenario": "other_brand_ean", "copies": "2"}  # 30 items: two pages of 20
    await _control(
        app, spapi_catalog={str(facts[k.product_id].identifier_value): spec for k in kids}
    )
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=False)
    flagged = {i.field for i in result.items if i.raw_code == "catalog:other_brand"}
    assert flagged == {f"variants[{k.seller_sku}].facts.identifier" for k in kids}
    assert result.failed == ()


async def test_a_partial_search_keeps_unflagged_units_out_of_scope() -> None:
    app = build_fake()
    sent, kids, facts = _paged_family(15)
    spec = {"scenario": "other_brand_ean", "copies": "2"}
    await _control(
        app,
        spapi_catalog={str(facts[k.product_id].identifier_value): spec for k in kids},
        spapi_catalog_page_5xx=True,
    )
    result = await amazon_adapter(app).precheck_catalog(sent, restrictions=False)
    flagged = {i.field for i in result.items}
    assert len(flagged) == 10  # page one: 20 items, two per barcode
    unflagged = [k for k in kids if f"variants[{k.seller_sku}].facts.identifier" not in flagged]
    assert len(unflagged) == 5 and set(result.failed) == {k.seller_sku for k in unflagged}
    for k in unflagged:
        assert f"variants[{k.seller_sku}].facts.identifier" not in result.scope
    assert flagged <= set(result.scope)
