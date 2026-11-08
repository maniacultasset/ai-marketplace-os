"""SI2 T10 (AC4, AC5, AC6, AC8, AC10): the Flipkart price block, compliance facts and images."""

from __future__ import annotations

import dataclasses
import logging
from decimal import Decimal
from typing import Any

import pytest
from app.adapters.flipkart import constants as c
from app.adapters.flipkart import mapping
from mkos_schemas.codes import ErrorCode
from tests.adapters.flipkart_support import (
    SELLER_FACTS,
    fk_family,
    fk_item,
    flipkart_adapter,
    listing,
)
from tests.support.fake_marketplace import build_fake

ADDRESS = "1 Mill Road, Surat, GJ, 395003, IN"


def _with(sent: Any, **facts: Any) -> Any:
    one = dataclasses.replace(SELLER_FACTS, **facts)
    return dataclasses.replace(sent, unit_facts={u.product_id: one for u in sent.units()})


def _attrs(sent: Any) -> dict[str, Any]:
    return mapping.build(sent)[0][0].payload["attributes"]  # type: ignore[no-any-return]


def _codes(errors: list[Any]) -> set[tuple[str, str]]:
    return {(e.field, e.error_code.value) for e in errors}


def _calls(app: Any) -> list[dict[str, Any]]:
    return [x for x in app.state.fake.calls if x["marketplace"] == "flipkart"]


def test_price_block_exact() -> None:
    attrs = _attrs(fk_item())
    assert attrs["price"] == [{"value": {"mrp": 999.0, "selling_price": 499.0, "currency": "INR"}}]
    assert Decimal(repr(attrs["price"][0]["value"]["mrp"])) == Decimal("999.00")


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"mrp": None}, ("facts.mrp", "MISSING_ATTRIBUTE")),
        ({"selling_price": Decimal("1000.00")}, ("facts.selling_price", "PRICE_ABOVE_MRP")),
        ({"currency": "USD"}, ("facts.currency", "VALUE_NOT_ALLOWED")),
        ({"mrp": Decimal("0.00")}, ("facts.mrp", "VALUE_NOT_ALLOWED")),
    ],
)
def test_price_errors(facts: dict[str, Any], expected: tuple[str, str]) -> None:
    sent = _with(fk_item(), **facts)
    assert expected in _codes(mapping.check(sent))
    assert "price" not in _attrs(sent)


def test_child_price_errors_at_variant_paths() -> None:
    family = fk_family()
    first = family.units()[0]
    facts = dict(family.unit_facts)
    facts[first.product_id] = dataclasses.replace(SELLER_FACTS, selling_price=None)
    errors = mapping.check(dataclasses.replace(family, unit_facts=facts))
    assert _codes(errors) == {
        (f"variants[{first.seller_sku}].facts.selling_price", "MISSING_ATTRIBUTE")
    }


@pytest.mark.parametrize("sent", [fk_item, fk_family], ids=["standalone", "family"])
def test_no_forbidden_key_in_any_payload(sent: Any) -> None:
    for item in mapping.build(sent())[0]:
        keys = {k.strip().casefold() for k in item.payload["attributes"]}
        assert not keys & c.FORBIDDEN_ATTRIBUTES
        assert "price" in keys


async def test_update_of_mrp_patches_price_only() -> None:
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_item(sku="FK-PRICE")
    await adapter.create_listing(sent)
    app.state.fake.calls.clear()
    result = await adapter.update_listing(sent, fields=["facts.mrp"])
    assert {f.field: f.status for f in result.fields} == {
        "facts.mrp": "succeeded",
        "facts.selling_price": "succeeded",
    }
    (call,) = _calls(app)
    assert call["path"].endswith("/listings/v3/update")


def test_reverse_index() -> None:
    from tests.adapters.flipkart_support import template

    index = mapping.reverse_index(template().template)
    assert index["price"] == ("facts.mrp", "facts.selling_price")
    assert index["Country of Origin"] == ("facts.country_of_origin",)
    assert index["Fabric Composition"] == ("facts.fiber_composition",)
    assert index["Manufacturer Details"] == ("facts.manufacturer",)


# ---- AC6 / AC5 ----


@pytest.mark.parametrize(("given", "name"), [("IN", "India"), ("India", "India")])
def test_country(given: str, name: str) -> None:
    attrs = _attrs(_with(fk_item(), country_of_origin=given))
    assert attrs["Country of Origin"] == [{"value": name}]


def test_country_not_allowed_and_composition() -> None:
    errors = mapping.check(_with(fk_item(), country_of_origin="XX"))
    assert ("facts.country_of_origin", "VALUE_NOT_ALLOWED") in _codes(errors)
    attrs = _attrs(fk_item())
    assert attrs["Fabric Composition"] == [{"value": "100% Cotton"}]
    assert attrs["Manufacturer Details"] == [{"value": f"Test Mills Pvt Ltd, {ADDRESS}"}]


async def test_reject_echoing_the_address_leaks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_fake()
    app.state.fake.fk_reject_attribute["Manufacturer Details"] = (
        "INVALID_VALUE",
        f"Invalid value 'Test Mills Pvt Ltd, {ADDRESS}'",
    )
    result = await flipkart_adapter(app).create_listing(fk_item(sku="FK-LEAK"))
    statuses = {f.field: f.status for f in result.fields}
    assert statuses["facts.manufacturer"] == "failed"
    text = result.model_dump_json() + caplog.text
    assert ADDRESS not in text and "Mill Road" not in text


# ---- AC8 ----


@pytest.mark.parametrize("colour", ["Mustard", "Peach"])
def test_new_colours_map(colour: str) -> None:
    attrs = _attrs(fk_item(listing(color=colour, fabric_type="Cotton Lycra")))
    assert attrs["Colour"] == [{"value": colour}]
    assert attrs["Fabric"] == [{"value": "Cotton Lycra"}]


def test_unmappable_colour_suggests() -> None:
    errors = mapping.check(fk_item(listing(color="Neon Plaid")))
    (bad,) = [e for e in errors if e.field == "attributes.color"]
    assert bad.error_code is ErrorCode.VALUE_NOT_ALLOWED and bad.suggestion


# ---- AC10 ----


def _refs(sent: Any, make: Any) -> Any:
    return dataclasses.replace(
        sent, image_refs={i.image_id: make(i.image_id) for i in sent.listing.images}
    )


def test_images_https_http_and_tags() -> None:
    ok = _refs(fk_item(), lambda i: f"https://images.kuinn.in/{i}.jpg")
    assert _attrs(ok)["images"][0]["value"].startswith("https://images.kuinn.in/")
    http = _refs(fk_item(), lambda i: f"http://localhost:9000/marketplace-images-public/{i}.jpg")
    assert ("images[0]", "VALUE_NOT_ALLOWED") in _codes(mapping.check(http))
    assert not any(
        e.field.startswith("images[")
        for e in mapping.check(dataclasses.replace(http, allow_http_images=True))
    )
    tagged = _refs(fk_item(), lambda i: f"image:{i}")
    assert ("images[0]", "VALUE_NOT_ALLOWED") in _codes(mapping.check(tagged))
    assert not any(e.field.startswith("images[") for e in mapping.preview(tagged).report.errors)
