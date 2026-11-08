"""SI2 T9 (AC11, AC13; P:§6): the reverse index, offer-only retry, no address in issue text, and
the per-role schema check."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import pytest
from app.adapters.amazon import mapping
from mkos_schemas.codes import ErrorCode
from tests.adapters.amazon_support import amazon_adapter, amazon_family, amazon_item, template
from tests.support.fake_marketplace import build_fake
from tests.unit.test_amazon_schema_check import kurta, ready

ADDRESS = "1 Mill Road, Surat, GJ, 395003, IN"
COMBINED = f"Test Mills Pvt Ltd, {ADDRESS}"


def _fields(result: Any) -> dict[str, str]:
    return {f.field: f.status for f in result.fields}


def test_reverse_index_offer_and_facts() -> None:
    index = mapping.reverse_index(template().template)
    assert index["purchasable_offer"] == ("facts.mrp", "facts.selling_price")
    assert index["manufacturer"] == ("facts.manufacturer",)
    assert index["manufacturer_contact_information"] == ("facts.manufacturer",)
    assert index["country_of_origin"] == ("facts.country_of_origin",)
    assert index["fabric_type"] == ("facts.fiber_composition",)
    assert index["material"] == ("attributes.fabric_type",)
    assert "condition_type" not in index


async def test_offer_rejected_content_accepted_then_offer_only_patch() -> None:
    app = build_fake()
    app.state.fake.spapi_reject["purchasable_offer"] = ("8560", "The price is invalid.")
    adapter = amazon_adapter(app)
    sent = amazon_item()
    result = await adapter.create_listing(sent)
    statuses = _fields(result)
    assert result.status == "partially_published"
    assert statuses["facts.mrp"] == statuses["facts.selling_price"] == "failed"
    assert statuses["content.title"] == "succeeded"
    app.state.fake.spapi_reject.clear()
    app.state.fake.calls.clear()
    retry = dataclasses.replace(sent, idempotency_key="retry-1")
    again = await adapter.update_listing(retry, fields={sent.seller_sku: ["facts.mrp"]})
    assert _fields(again) == {"facts.mrp": "succeeded", "facts.selling_price": "succeeded"}
    (call,) = app.state.fake.calls
    assert call["method"] == "PATCH"


async def test_offer_patch_body_is_the_offer_only() -> None:
    seen: list[dict[str, Any]] = []
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = amazon_item()
    await adapter.create_listing(sent)
    original = adapter._client.patch

    async def spy(sku: str, body: dict[str, Any]) -> Any:
        seen.append(body)
        return await original(sku, body)

    adapter._client.patch = spy
    await adapter.update_listing(sent, fields=["facts.selling_price"])
    (body,) = seen
    assert [p["path"] for p in body["patches"]] == ["/attributes/purchasable_offer"]
    assert all(p["op"] == "replace" for p in body["patches"])


async def test_unknown_fact_path_refused() -> None:
    family = amazon_family()
    parent = family.units()[0]  # a parent carries no price path
    with pytest.raises(ValueError, match="does not carry"):
        await amazon_adapter(build_fake()).update_listing(
            family, fields={parent.seller_sku: ["facts.mrp"]}
        )


async def test_issue_text_never_carries_the_address(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_fake()
    app.state.fake.spapi_reject["manufacturer_contact_information"] = (
        "90244",
        f"Invalid value '{COMBINED}'",
    )
    result = await amazon_adapter(app).create_listing(amazon_item())
    assert _fields(result)["facts.manufacturer"] == "failed"
    text = result.model_dump_json() + caplog.text
    assert ADDRESS not in text and COMBINED not in text and "Mill Road" not in text


async def test_listing_schema_flags_child_not_parent() -> None:
    listing_schema = kurta()
    listing_schema["required"] = sorted({*listing_schema["required"], "purchasable_offer"})
    listing_schema["properties"]["purchasable_offer"] = {"type": "array"}
    sellable = await ready(listing_schema)
    parent_only = await ready(kurta())
    family = amazon_family()
    _parent, first, *_rest = family.units()
    facts = dict(family.unit_facts)
    facts[first.product_id] = dataclasses.replace(facts[first.product_id], mrp=None)
    sent = dataclasses.replace(
        family,
        unit_facts=facts,
        product_type_schema=sellable,
        parent_type_schema=parent_only,
    )
    errors = mapping.check(sent)
    required = [e for e in errors if (e.raw_code or "").startswith("ptd:required")]
    assert required
    assert any(e.error_code is ErrorCode.MISSING_ATTRIBUTE for e in required)
    clean = dataclasses.replace(
        family, product_type_schema=sellable, parent_type_schema=parent_only
    )
    assert [e for e in mapping.check(clean) if (e.raw_code or "").startswith("ptd:")] == []
    none = dataclasses.replace(family, product_type_schema=None, parent_type_schema=None)
    assert [e for e in mapping.check(none) if (e.raw_code or "").startswith("ptd:")] == []
    assert json.dumps([e.model_dump(mode="json") for e in errors]).count("Mill Road") == 0
