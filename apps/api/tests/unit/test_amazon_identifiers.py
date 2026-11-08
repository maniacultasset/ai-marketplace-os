"""SI5 B2 (AC22, AC23; PLAN §2.7-2.8, D17; CP-D14): the Amazon barcode or GTIN exemption on
sellable items, and the refusal of a create that has neither."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from app.adapters.amazon import adapter as amazon_adapter_module
from app.adapters.amazon import constants as c
from app.adapters.amazon import mapping
from app.adapters.amazon.drift import diff, sellable_names
from app.adapters.base import AdapterListing, SellerFacts
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import RetryClass
from tests.adapters.amazon_support import (
    SELLER_FACTS,
    amazon_adapter,
    amazon_family,
    amazon_item,
    template,
)
from tests.support.fake_marketplace import build_fake, ptd_fixture
from tests.unit.test_amazon_schema_check import ready

EAN = "4006381333931"
BARE = dataclasses.replace(SELLER_FACTS, identifier_type=None, identifier_value=None)
EXEMPT = dataclasses.replace(BARE, identifier_exempt=True)
MID = mapping.DEFAULT_MARKETPLACE_ID


def _ean(value: str = EAN, id_type: str = "ean") -> SellerFacts:
    return dataclasses.replace(BARE, identifier_type=id_type, identifier_value=value)


def _standalone(facts: SellerFacts) -> AdapterListing:
    sent = amazon_item()
    return dataclasses.replace(sent, unit_facts={sent.listing.product_id: facts})


def _family(first: SellerFacts, rest: SellerFacts | None = None) -> AdapterListing:
    """A family whose first child has `first` and every other unit `rest` (an EAN)."""
    sent = amazon_family()
    parent, kid, *kids = sent.units()
    others = rest or _ean()
    facts = {u.product_id: others for u in (parent, *kids)}
    facts[kid.product_id] = first
    return dataclasses.replace(sent, unit_facts=facts)


def _attrs(item: Any) -> dict[str, Any]:
    return item.payload["attributes"]  # type: ignore[no-any-return]


def _identifier_errors(errors: Any) -> list[Any]:
    return [e for e in errors if e.error_code is ErrorCode.IDENTIFIER_MISSING]


# ---- AC22: what is sent ----


def test_standalone_ean_is_sent() -> None:
    items, errors = mapping.build(_standalone(_ean()))
    assert errors == []
    (item,) = items
    attrs = _attrs(item)
    assert attrs[c.EXTERNAL_ID_ATTRIBUTE] == [{"type": "ean", "value": EAN, "marketplace_id": MID}]
    assert c.EXEMPTION_ATTRIBUTE not in attrs


def test_exempt_child_sends_the_flag_and_the_parent_neither() -> None:
    sent = _family(EXEMPT)
    items, errors = mapping.build(sent)
    assert errors == []
    parent, first, *rest = items
    assert c.EXTERNAL_ID_ATTRIBUTE not in _attrs(parent)
    assert c.EXEMPTION_ATTRIBUTE not in _attrs(parent)
    assert _attrs(first)[c.EXEMPTION_ATTRIBUTE] == [{"value": True, "marketplace_id": MID}]
    assert c.EXTERNAL_ID_ATTRIBUTE not in _attrs(first)
    for child in rest:
        (entry,) = _attrs(child)[c.EXTERNAL_ID_ATTRIBUTE]
        assert entry == {"type": "ean", "value": EAN, "marketplace_id": MID}
        assert c.EXEMPTION_ATTRIBUTE not in _attrs(child)


def test_a_parent_with_identifier_facts_still_sends_neither() -> None:
    sent = amazon_family()
    sent = dataclasses.replace(sent, unit_facts={u.product_id: _ean() for u in sent.units()})
    parent = mapping.build(sent)[0][0]
    assert parent.unit.item_role == "parent"
    assert c.EXTERNAL_ID_ATTRIBUTE not in _attrs(parent)
    assert c.EXEMPTION_ATTRIBUTE not in _attrs(parent)


@pytest.mark.parametrize(
    ("id_type", "value"),
    [
        ("ean", "4006381333931"),
        ("ean", "96385074"),
        ("upc", "012345678905"),
        ("gtin", "00012345678905"),
    ],
)
def test_each_type_maps_and_leading_zeros_stay(id_type: str, value: str) -> None:
    (item,) = mapping.build(_standalone(_ean(value, id_type)))[0]
    (entry,) = _attrs(item)[c.EXTERNAL_ID_ATTRIBUTE]
    assert entry["type"] == c.IDENTIFIER_TYPES[id_type]
    assert isinstance(entry["value"], str) and entry["value"] == value


def test_barcode_wins_over_a_stray_exemption() -> None:
    (item,) = mapping.build(_standalone(dataclasses.replace(_ean(), identifier_exempt=True)))[0]
    assert c.EXTERNAL_ID_ATTRIBUTE in _attrs(item)
    assert c.EXEMPTION_ATTRIBUTE not in _attrs(item)


# ---- AC23: a create with neither is refused before the send ----


def test_family_with_one_bare_child_is_blocked_at_its_path() -> None:
    sent = _family(BARE)
    bare = sent.units()[1]
    errors = mapping.check(sent)
    assert [(e.field, e.error_code) for e in errors] == [
        (f"variants[{bare.seller_sku}].facts.identifier", ErrorCode.IDENTIFIER_MISSING)
    ]


def test_bare_standalone_is_blocked_at_facts_identifier() -> None:
    (problem,) = mapping.check(_standalone(BARE))
    assert (problem.field, problem.error_code) == ("facts.identifier", ErrorCode.IDENTIFIER_MISSING)
    assert problem.retry_class is RetryClass.USER_ACTION


def test_no_unit_facts_is_blocked_too() -> None:
    sent = dataclasses.replace(amazon_item(), unit_facts={})
    assert [e.field for e in _identifier_errors(mapping.check(sent))] == ["facts.identifier"]


def test_message_names_the_sku_and_the_exemption_never_a_value() -> None:
    sent = _family(BARE)
    bare = sent.units()[1]
    (problem,) = _identifier_errors(mapping.check(sent))
    assert problem.message == (
        f"Amazon needs a barcode (EAN, UPC or GTIN) for {bare.seller_sku}, or mark it GTIN-exempt"
        " if Amazon approved an exemption for your brand."
    )
    assert problem.value_submitted is None and problem.suggestion is None
    assert EAN not in problem.message


def test_preview_report_shows_the_item() -> None:
    sent = _family(BARE)
    bare = sent.units()[1]
    report = mapping.preview(sent).report
    assert report.status == "failed"
    assert report.error_code is ErrorCode.IDENTIFIER_MISSING
    assert [e.field for e in _identifier_errors(report.errors)] == [
        f"variants[{bare.seller_sku}].facts.identifier"
    ]


async def test_create_with_a_bare_child_sends_nothing() -> None:
    app = build_fake()
    sent = _family(BARE)
    bare = sent.units()[1]
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "failed"
    assert app.state.fake.calls == []
    assert result.error is not None
    assert [e.field for e in _identifier_errors(result.error.errors)] == [
        f"variants[{bare.seller_sku}].facts.identifier"
    ]


# ---- CP-D14: an update of a live item is never refused for it ----


def test_build_without_the_requirement_has_no_identifier_error() -> None:
    _, errors = mapping.build(_family(BARE), require_identifier=False)
    assert _identifier_errors(errors) == []


async def test_update_with_a_bare_child_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    live = _family(_ean())
    assert (await adapter.create_listing(live)).status == "published"
    app.state.fake.calls.clear()
    seen: list[Any] = []
    real = mapping.build

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("require_identifier", True))
        return real(*args, **kwargs)

    monkeypatch.setattr(amazon_adapter_module, "build", spy)
    child = live.units()[1]
    facts = {u.product_id: _ean() for u in live.units()}
    facts[child.product_id] = BARE
    bare = dataclasses.replace(live, unit_facts=facts, idempotency_key="update-1")
    result = await adapter.update_listing(bare, fields={child.seller_sku: ["content.title"]})
    assert seen == [False]
    assert result.status == "published"
    assert {call["method"] for call in app.state.fake.calls} == {"PATCH"}
    for one in result.items:
        assert all(
            f.error is None or f.error.error_code is not ErrorCode.IDENTIFIER_MISSING
            for f in one.fields
        )


# ---- the stored LISTING definition and the drift check ----


@pytest.mark.parametrize("facts", [_ean(), EXEMPT])
async def test_both_shapes_pass_the_stored_listing_definition(facts: SellerFacts) -> None:
    sellable = await ready(json.loads(ptd_fixture("KURTA.listing.schema.json")))
    sent = dataclasses.replace(_standalone(facts), product_type_schema=sellable)
    _, errors = mapping.build(sent)
    assert errors == [], [e.model_dump(mode="json") for e in errors]


async def test_a_wrong_shape_fails_the_stored_listing_definition() -> None:
    sellable = await ready(json.loads(ptd_fixture("KURTA.listing.schema.json")))
    sent = dataclasses.replace(_standalone(_ean("ABC")), product_type_schema=sellable)
    _, errors = mapping.build(sent)
    assert any(c.EXTERNAL_ID_ATTRIBUTE in e.message for e in errors)


def test_identifier_names_are_drift_checked() -> None:
    assert {c.EXTERNAL_ID_ATTRIBUTE, c.EXEMPTION_ATTRIBUTE} <= set(sellable_names())
    schema: dict[str, Any] = json.loads(ptd_fixture("KURTA.listing.schema.json"))
    templates = [template().template]
    assert diff(schema, templates, "KURTA") == []
    schema["required"] = [*schema["required"], c.EXTERNAL_ID_ATTRIBUTE]
    assert diff(schema, templates, "KURTA") == []  # it is fed, so a required one is no drift
