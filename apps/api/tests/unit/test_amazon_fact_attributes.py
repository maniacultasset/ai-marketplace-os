"""SI2 T8 (AC5, AC6, AC8, AC10): Amazon compliance facts, suggestions and image locators."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Any

import pytest
from app.adapters.amazon import mapping
from app.adapters.base import AdapterTemplate, SellerFacts
from mkos_schemas.codes import ErrorCode
from tests.adapters.amazon_support import (
    SELLER_FACTS,
    amazon_adapter,
    amazon_item,
    listing,
    template,
)
from tests.support.fake_marketplace import build_fake

ADDRESS = "1 Mill Road, Surat, GJ, 395003, IN"


def _with(sent: Any, **facts: Any) -> Any:
    one = dataclasses.replace(SELLER_FACTS, **facts)
    return dataclasses.replace(sent, unit_facts={u.product_id: one for u in sent.units()})


def _attrs(sent: Any) -> dict[str, Any]:
    (item,) = mapping.build(sent)[0]
    return item.payload["attributes"]  # type: ignore[no-any-return]


def _codes(errors: list[Any]) -> set[tuple[str, str]]:
    return {(e.field, e.error_code.value) for e in errors}


def _value(attrs: dict[str, Any], name: str) -> Any:
    return attrs[name][0]["value"]


# ---- AC5: the manufacturer ----


def test_manufacturer_and_contact() -> None:
    attrs = _attrs(amazon_item())
    assert _value(attrs, "manufacturer") == "Test Mills Pvt Ltd"
    assert _value(attrs, "manufacturer_contact_information") == f"Test Mills Pvt Ltd, {ADDRESS}"


def test_no_manufacturer_blocks() -> None:
    errors = mapping.check(_with(amazon_item(), manufacturer_name=None, manufacturer_address=None))
    assert ("facts.manufacturer", "MISSING_ATTRIBUTE") in _codes(errors)


async def test_address_never_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sent = _with(amazon_item(), mrp=None, country_of_origin="XX")
    report = mapping.preview(sent).report
    result = await amazon_adapter(build_fake()).create_listing(sent)
    text = report.model_dump_json() + result.model_dump_json() + caplog.text
    assert "Mill Road" not in text and ADDRESS not in text


# ---- AC6: country and composition ----


@pytest.mark.parametrize(("given", "sent"), [("IN", "IN"), ("India", "IN"), ("bharat", "IN")])
def test_country_mapped(given: str, sent: str) -> None:
    assert (
        _value(_attrs(_with(amazon_item(), country_of_origin=given)), "country_of_origin") == sent
    )


def test_country_not_allowed() -> None:
    errors = mapping.check(_with(amazon_item(), country_of_origin="XX"))
    (bad,) = [e for e in errors if e.field == "facts.country_of_origin"]
    assert bad.error_code is ErrorCode.VALUE_NOT_ALLOWED and bad.value_submitted == "XX"
    assert bad.suggestion and bad.suggestion.startswith("Allowed values include: ")


def test_no_country_blocks() -> None:
    errors = mapping.check(_with(amazon_item(), country_of_origin=None))
    assert ("facts.country_of_origin", "MISSING_ATTRIBUTE") in _codes(errors)


def test_composition_parts_trim_and_sum() -> None:
    parts = [{"fiber": "cotton", "percent": 60}, {"fiber": "polyester", "percent": 40}]
    sent = _with(amazon_item(), fiber_composition_text=None, fiber_composition_parts=parts)
    assert _value(_attrs(sent), "fabric_type") == "60% Cotton, 40% Polyester"
    trimmed = _with(amazon_item(), fiber_composition_text=" 100% Cotton ")
    assert _value(_attrs(trimmed), "fabric_type") == "100% Cotton"
    bad = [{"fiber": "cotton", "percent": 90}]
    errors = mapping.check(
        _with(amazon_item(), fiber_composition_text=None, fiber_composition_parts=bad)
    )
    (one,) = [e for e in errors if e.field == "facts.fiber_composition"]
    assert one.error_code is ErrorCode.VALUE_NOT_ALLOWED and one.value_submitted is None


def test_canonical_fabric_goes_out_as_material() -> None:
    attrs = _attrs(amazon_item(listing(fabric_type="Cotton Lycra")))
    assert _value(attrs, "material") == "Cotton Lycra"


def test_tshirt_weight_still_sent_after_overlay() -> None:
    from app.core.resolved_facts import ProductFacts
    from app.services.seller_facts import seller_overlay

    shirt = template("tshirts")
    body = listing(product_weight=999)
    facts = ProductFacts(
        product_id=body.product_id, sku="S", level="standalone", product_weight_g=180
    )
    body = seller_overlay(body, facts, shirt.template)
    attrs = _attrs(amazon_item(body, template=shirt))
    assert attrs["item_weight"][0]["value"] == 180


def test_old_template_without_fact_specs_still_requires_them() -> None:
    old = template()
    specs = {k: v for k, v in old.template.specs.items() if k not in ("manufacturer",)}
    v2 = AdapterTemplate(old.product_type, None, old.template.model_copy(update={"specs": specs}))
    attrs = _attrs(amazon_item(template=v2))
    assert "manufacturer" not in attrs and "purchasable_offer" in attrs
    errors = mapping.check(
        _with(amazon_item(template=v2), manufacturer_name=None, manufacturer_address=None)
    )
    assert ("facts.manufacturer", "MISSING_ATTRIBUTE") in _codes(errors)


# ---- AC8: suggestions ----


def test_unmappable_colour_has_a_suggestion() -> None:
    errors = mapping.check(amazon_item(listing(color="Neon Plaid")))
    (bad,) = [e for e in errors if e.field == "attributes.color"]
    assert bad.error_code is ErrorCode.VALUE_NOT_ALLOWED and bad.value_submitted == "Neon Plaid"
    assert bad.suggestion and bad.suggestion.startswith("Allowed values include: ")
    assert 1 <= len(bad.suggestion.removeprefix("Allowed values include: ").split(", ")) <= 5


# ---- AC10: image locators ----


def _refs(sent: Any, make: Any) -> Any:
    return dataclasses.replace(
        sent, image_refs={i.image_id: make(i.image_id) for i in sent.listing.images}
    )


def test_https_images_in_order() -> None:
    sent = _refs(amazon_item(), lambda i: f"https://images.kuinn.in/{i}.jpg")
    attrs = _attrs(sent)
    first = sent.listing.images[0].image_id
    assert attrs["main_product_image_locator"][0]["media_location"].endswith(f"{first}.jpg")
    if len(sent.listing.images) > 1:
        last = sent.listing.images[-1].image_id
        name = f"other_product_image_locator_{len(sent.listing.images) - 1}"
        assert attrs[name][0]["media_location"].endswith(f"{last}.jpg")


def test_http_image_blocks_unless_allowed() -> None:
    sent = _refs(
        amazon_item(), lambda i: f"http://localhost:9000/marketplace-images-public/{i}.jpg"
    )
    codes = _codes(mapping.check(sent))
    assert ("images[0]", "VALUE_NOT_ALLOWED") in codes
    local = dataclasses.replace(sent, allow_http_images=True)
    assert not any(f.startswith("images[") for f, _ in _codes(mapping.check(local)))


@pytest.mark.parametrize("bad", ["/relative.jpg", "javascript:alert(1)", "image:abc"])
def test_bad_image_refs_block(bad: str) -> None:
    sent = _refs(amazon_item(), lambda i: bad)
    assert ("images[0]", "VALUE_NOT_ALLOWED") in _codes(mapping.check(sent))


def test_preview_tag_passes_only_in_preview() -> None:
    sent = _refs(amazon_item(), lambda i: f"image:{i}")
    assert not any(e.field.startswith("images[") for e in mapping.preview(sent).report.errors)
    assert ("images[0]", "VALUE_NOT_ALLOWED") in _codes(mapping.check(sent))


async def test_local_http_refs_reach_the_fake_and_tags_send_nothing() -> None:
    app = build_fake()
    local = dataclasses.replace(
        _refs(amazon_item(), lambda i: f"http://localhost:9000/marketplace-images-public/{i}.jpg"),
        allow_http_images=True,
    )
    result = await amazon_adapter(app).create_listing(local)
    assert result.status != "failed" and len(app.state.fake.calls) == 1
    app2 = build_fake()
    tagged = _refs(amazon_item(), lambda i: f"image:{i}")
    assert (await amazon_adapter(app2).create_listing(tagged)).status == "failed"
    assert app2.state.fake.calls == []


def test_tenth_image_skipped_without_error() -> None:
    from mkos_schemas.listing import ImageRef

    body = amazon_item().listing
    proto = body.images[0]
    images = [proto.model_copy(update={"image_id": uuid.uuid4(), "position": n}) for n in range(10)]
    sent = _refs(
        amazon_item(body.model_copy(update={"images": images})), lambda i: f"https://x.test/{i}.jpg"
    )
    items, errors = mapping.build(sent)
    assert "images[9]" in items[0].skipped
    assert not any(e.field.startswith("images[") for e in errors)
    assert isinstance(proto, ImageRef)


def test_seller_facts_type() -> None:
    assert isinstance(SELLER_FACTS, SellerFacts)
