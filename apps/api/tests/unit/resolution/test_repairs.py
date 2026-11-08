"""SI11 T9-T10 (AC5, AC6, AC15; P:B2-B3, D8-D9): Amazon's pure repair rules, the alias table,
and `apply_repairs` (the digest guard)."""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon.aliases import ALIASES_VERSION
from app.adapters.amazon.repairs import (
    REFUSED_ATTRIBUTES,
    RepairContext,
    apply_repairs,
    as_adapter_repair,
    digest,
    fold,
    gtin_ok,
    propose,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem
from tests.adapters.amazon_support import SELLER_FACTS, amazon_family, amazon_item, listing

MID = "A21TJRUUN4KGV"


def _v(repair: Any) -> Any:
    assert repair is not None and repair.value is not None
    return repair.value


def _issue(field: str = "attributes.color", code: str = "99300") -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.ERROR,
        message="m",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code=code,
    )


def _sent(name: str, value: Any) -> dict[str, Any]:
    return {name: [{"value": value, "marketplace_id": MID, "language_tag": "en_IN"}]}


def _propose(attribute: str, value: Any, *, enum: tuple[str, ...] = (), **ctx: Any) -> Any:
    sent = amazon_item()
    unit = sent.units()[0]
    context = RepairContext(enums={attribute: enum}, **ctx)
    return propose(
        sent, unit, _issue(f"attributes.{attribute}"), attribute, _sent(attribute, value), context
    )


# ---- enum_fold ----


@pytest.mark.parametrize(
    ("value", "enum", "want"),
    [
        (" red ", ("Red", "Blue"), "Red"),
        ("navy", ("Navy Blue", "Navy"), "Navy"),
        ("\uff32\uff45\uff44", ("Red",), "Red"),  # full width folds under NFKC
        ("red   blue", ("Red Blue",), "Red Blue"),
    ],
)
def test_enum_fold_exact(value: str, enum: tuple[str, ...], want: str) -> None:
    repair = _propose("color", value, enum=enum)
    assert repair is not None and repair.rule == "enum_fold"
    assert _v(repair)[0]["value"] == want
    assert repair.alias_version is None  # an exact fold never records the alias table


@pytest.mark.parametrize(
    ("value", "enum"),
    [
        ("navy", ("Navy", "NAVY")),  # two folds: never pick one
        ("", ("Red",)),
        ("   ", ("Red",)),
        ("Red", ("Red", "Blue")),  # already right: nothing to change
        ("teal", ("Red", "Blue")),  # no match: never "nearest"
        ("red", ()),  # no enum (a free-text attribute)
    ],
)
def test_enum_fold_refuses(value: str, enum: tuple[str, ...]) -> None:
    assert _propose("color", value, enum=enum) is None


def test_size_alias_xxl_to_2xl() -> None:
    repair = _propose("size", "XXL", enum=("S", "M", "L", "XL", "2XL"))
    assert repair is not None
    assert (_v(repair)[0]["value"], repair.alias_version) == ("2XL", ALIASES_VERSION)


def test_alias_with_two_targets_in_the_enum_refuses() -> None:
    assert _propose("color", "wine", enum=("Maroon", "Burgundy")) is None


def test_colour_alias_needs_an_enum() -> None:
    assert _propose("color", "jet black", enum=()) is None
    hit = _propose("color", "jet black", enum=("Black", "White"))
    assert hit is not None and _v(hit)[0]["value"] == "Black"


# ---- unit_format, identifier_normalize ----


def test_unit_format() -> None:
    repair = _propose("item_length", "12.5 cm", units={"item_length": ("centimeters",)})
    assert repair is not None and repair.rule == "unit_format"
    assert (_v(repair)[0]["value"], _v(repair)[0]["unit"]) == (12.5, "centimeters")


@pytest.mark.parametrize("value", ["NaN cm", "inf cm", "-3 cm", "12.5 furlongs", "12.5"])
def test_unit_format_refuses(value: str) -> None:
    assert _propose("item_length", value, units={"item_length": ("centimeters",)}) is None


def test_identifier_normalize() -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    entry = {"type": "ean", "value": "4 006381-333931", "marketplace_id": MID}
    repair = propose(
        sent, unit, _issue("listing"), c.EXTERNAL_ID_ATTRIBUTE,
        {c.EXTERNAL_ID_ATTRIBUTE: [entry]}, RepairContext(),
    )  # fmt: skip
    assert repair is not None and _v(repair)[0]["value"] == "4006381333931"
    bad = {**entry, "value": "4 006381-333932"}
    assert (
        propose(
            sent,
            unit,
            _issue("listing"),
            c.EXTERNAL_ID_ATTRIBUTE,
            {c.EXTERNAL_ID_ATTRIBUTE: [bad]},
            RepairContext(),
        )
        is None
    )
    assert gtin_ok("4006381333931") and not gtin_ok("4006381333932")


# ---- exemption, fill_from_facts, drop_locked, image_slot ----


def test_exemption_flag_only_when_the_seller_is_exempt() -> None:
    exempt = dataclasses.replace(
        SELLER_FACTS, identifier_type=None, identifier_value=None, identifier_exempt=True
    )
    sent = amazon_item(unit_facts={})
    sent = dataclasses.replace(sent, unit_facts={sent.units()[0].product_id: exempt})
    unit = sent.units()[0]
    repair = propose(sent, unit, _issue("listing"), c.EXEMPTION_ATTRIBUTE, {}, RepairContext())
    assert repair is not None and _v(repair)[0]["value"] is True
    plain = amazon_item()
    assert (
        propose(
            plain, plain.units()[0], _issue("listing"), c.EXEMPTION_ATTRIBUTE, {}, RepairContext()
        )
        is None
    )


def test_fill_from_facts_country_of_origin() -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    repair = propose(
        sent, unit, _issue("listing", "90220"), "country_of_origin", {}, RepairContext()
    )
    assert repair is not None and repair.rule == "fill_from_facts"
    assert _v(repair)[0]["value"] == "IN" and repair.field_path == "facts.country_of_origin"
    blank = dataclasses.replace(SELLER_FACTS, country_of_origin=None)
    empty = dataclasses.replace(sent, unit_facts={unit.product_id: blank})
    assert (
        propose(empty, unit, _issue("listing", "90220"), "country_of_origin", {}, RepairContext())
        is None
    )


def test_manufacturer_is_never_filled_or_repaired() -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    for name in ("manufacturer", c.MANUFACTURER_CONTACT):
        assert propose(sent, unit, _issue("listing"), name, {}, RepairContext()) is None


def test_drop_locked_only_on_4005033_of_a_stored_sku() -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    attrs = _sent("item_type_keyword", "kurtas")
    locked = _issue("listing", "4005033")
    assert propose(sent, unit, locked, "item_type_keyword", attrs, RepairContext()) is None
    repair = propose(sent, unit, locked, "item_type_keyword", attrs, RepairContext(stored=True))
    assert repair is not None and (repair.rule, repair.value) == ("drop_locked", None)
    other = _issue("listing", "99300")
    assert (
        propose(sent, unit, other, "item_type_keyword", attrs, RepairContext(stored=True)) is None
    )


def test_image_slot_picks_another_passing_image() -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    first, second = "https://img.test/a.jpg", "https://img.test/b.jpg"
    attrs = {c.MAIN_IMAGE: [{"media_location": first, "marketplace_id": MID}]}
    ctx = RepairContext(main_ok_images=((uuid.uuid4(), first), (uuid.uuid4(), second)))
    repair = propose(sent, unit, _issue("images[0]"), c.MAIN_IMAGE, attrs, ctx)
    assert repair is not None and _v(repair)[0]["media_location"] == second
    only = RepairContext(main_ok_images=((uuid.uuid4(), first),))
    assert propose(sent, unit, _issue("images[0]"), c.MAIN_IMAGE, attrs, only) is None


# ---- refused attributes (AC5): price, MRP, text, keywords never change ----


@pytest.mark.parametrize("attribute", sorted(REFUSED_ATTRIBUTES))
def test_refused_attributes(attribute: str) -> None:
    ctx = RepairContext(
        enums={attribute: ("Red",)}, units={attribute: ("centimeters",)}, stored=True
    )
    sent = amazon_item()
    unit = sent.units()[0]
    for code in ("99300", "4005033", "90220"):
        assert (
            propose(sent, unit, _issue("listing", code), attribute, _sent(attribute, " red "), ctx)
            is None
        )


# ---- property: one attribute, deterministic, idempotent ----

ATTRIBUTES = st.sampled_from(["color", "size", "item_length", *sorted(REFUSED_ATTRIBUTES)])
VALUES = st.one_of(
    st.text(max_size=20), st.sampled_from(["XXL", " red ", "12 cm", "jet black", "navy"])
)
ENUMS = st.lists(st.sampled_from(["Red", "Blue", "2XL", "XL", "Black", "Navy", "RED"]), max_size=4)


@settings(max_examples=150, deadline=None)
@given(attribute=ATTRIBUTES, value=VALUES, enum=ENUMS)
def test_property_one_attribute_deterministic_idempotent(
    attribute: str, value: str, enum: list[str]
) -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    ctx = RepairContext(enums={attribute: tuple(enum)}, units={attribute: ("centimeters",)})
    attrs = {**_sent(attribute, value), **_sent("item_name", "Kurta")}
    first = propose(sent, unit, _issue(f"attributes.{attribute}"), attribute, attrs, ctx)
    again = propose(sent, unit, _issue(f"attributes.{attribute}"), attribute, attrs, ctx)
    assert first == again
    if first is None:
        return
    assert first.attribute == attribute and attribute not in REFUSED_ATTRIBUTES
    applied = dataclasses.replace(sent, repairs=(as_adapter_repair(first, uuid.uuid4()),))
    once = apply_repairs(attrs, applied, unit)
    twice = apply_repairs(once.attributes, applied, unit)
    assert once.attributes == twice.attributes
    changed = {k for k in attrs if attrs.get(k) != once.attributes.get(k)}
    assert changed <= {attribute}


# ---- apply_repairs: the digest guard (AC6) and per-SKU ----


def _color_repair(
    sent: Any, value: str = "Red", *, sku: str | None = None, source: Any = "red"
) -> Any:
    return as_adapter_repair(
        dataclasses.replace(
            _propose("color", "red", enum=("Red",)),
            value=({"value": value, "marketplace_id": MID, "language_tag": "en_IN"},),
            source_digest=digest(source),
            seller_sku=sku,
        ),
        uuid.uuid4(),
    )


def test_apply_replaces_while_the_source_is_unchanged() -> None:
    body = listing(color="red")
    sent = amazon_item(body)
    repair = _color_repair(sent)
    applied = dataclasses.replace(sent, repairs=(repair,))
    result = apply_repairs(_sent("color", "red"), applied, applied.units()[0])
    assert result.attributes["color"][0]["value"] == "Red"
    ((rid, path, before, after),) = result.repaired
    assert (rid, path) == (repair.id, "attributes.color") and before != after
    assert result.stale == ()


def test_apply_reports_stale_after_a_seller_edit() -> None:
    sent = amazon_item(listing(color="blue"))  # the seller changed the colour since
    applied = dataclasses.replace(sent, repairs=(_color_repair(sent),))
    result = apply_repairs(_sent("color", "blue"), applied, applied.units()[0])
    assert result.attributes["color"][0]["value"] == "blue"
    assert len(result.stale) == 1 and result.repaired == ()


def test_apply_child_only() -> None:
    fam = amazon_family(listing(color="red"))
    child = fam.units()[1]
    other = fam.units()[2]
    repair = _color_repair(fam, sku=child.seller_sku, source=None)
    applied = dataclasses.replace(fam, repairs=(repair,))
    from app.adapters.amazon.repairs import source_value

    repair = dataclasses.replace(
        repair, source_digest=digest(source_value(applied, child, "attributes.color"))
    )
    applied = dataclasses.replace(fam, repairs=(repair,))
    assert apply_repairs(_sent("color", "x"), applied, child).repaired
    assert apply_repairs(_sent("color", "x"), applied, other).attributes["color"][0]["value"] == "x"


def test_drop_locked_removes_the_attribute() -> None:
    sent = amazon_item()
    unit = sent.units()[0]
    attrs = {**_sent("item_type_keyword", "kurtas"), **_sent("item_name", "K")}
    repair = propose(
        sent,
        unit,
        _issue("listing", "4005033"),
        "item_type_keyword",
        attrs,
        RepairContext(stored=True),
    )
    assert repair is not None
    applied = dataclasses.replace(sent, repairs=(as_adapter_repair(repair, uuid.uuid4()),))
    result = apply_repairs(attrs, applied, unit)
    assert "item_type_keyword" not in result.attributes and "item_name" in result.attributes


def test_no_repair_payload_is_unchanged() -> None:
    from app.adapters.amazon.mapping import build

    sent = amazon_item()
    (plain,), _ = build(sent)
    (again,), _ = build(dataclasses.replace(sent, repairs=()))
    assert plain.payload == again.payload and plain.repaired == ()


def test_fold_and_digest_are_stable() -> None:
    assert fold("  \uff32\uff45\uff44   Blue ") == "red blue"
    assert digest({"b": 1, "a": [Decimal("1.5")]}) == digest({"a": [Decimal("1.5")], "b": 1})
