"""SI5 B2 attacks (AC22, AC23; PLAN §2.7-2.8): every identifier combination on every role, two
bare children, blank or unknown values, and Flipkart left untouched (T39, flag off)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from app.adapters.amazon import constants as c
from app.adapters.amazon import mapping
from app.adapters.base import SellerFacts
from app.adapters.flipkart import mapping as fk_mapping
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from tests.adapters.amazon_support import SELLER_FACTS, amazon_family, amazon_item
from tests.adapters.flipkart_support import fk_family, fk_item

EAN = "4006381333931"
BARE = dataclasses.replace(SELLER_FACTS, identifier_type=None, identifier_value=None)


def _ids(errors: Any) -> list[Any]:
    return [e for e in errors if e.error_code is ErrorCode.IDENTIFIER_MISSING]


def test_two_bare_children_are_each_reported_at_their_own_path() -> None:
    sent = amazon_family()
    _parent, first, second, *_rest = sent.units()
    facts = {u.product_id: SELLER_FACTS for u in sent.units()}
    facts[first.product_id] = BARE
    facts[second.product_id] = BARE
    errors = mapping.check(dataclasses.replace(sent, unit_facts=facts))
    assert sorted(e.field for e in _ids(errors)) == sorted(
        f"variants[{u.seller_sku}].facts.identifier" for u in (first, second)
    )


def test_a_blank_value_is_not_a_barcode() -> None:
    sent = amazon_item()
    blank = dataclasses.replace(BARE, identifier_type="ean", identifier_value="   ")
    sent = dataclasses.replace(sent, unit_facts={sent.listing.product_id: blank})
    items, errors = mapping.build(sent)
    assert c.EXTERNAL_ID_ATTRIBUTE not in items[0].payload["attributes"]
    assert [e.field for e in _ids(errors)] == ["facts.identifier"]


def test_an_unknown_type_is_never_sent_under_a_guessed_type() -> None:
    sent = amazon_item()
    odd = dataclasses.replace(BARE, identifier_type="EAN", identifier_value=EAN)
    sent = dataclasses.replace(sent, unit_facts={sent.listing.product_id: odd})
    items, errors = mapping.build(sent)
    assert c.EXTERNAL_ID_ATTRIBUTE not in items[0].payload["attributes"]
    assert [e.field for e in _ids(errors)] == ["facts.identifier"]


def test_a_bare_parent_with_barcoded_children_is_not_refused() -> None:
    sent = amazon_family()
    parent = sent.units()[0]
    facts = {u.product_id: SELLER_FACTS for u in sent.units()}
    facts[parent.product_id] = BARE
    assert _ids(mapping.check(dataclasses.replace(sent, unit_facts=facts))) == []


def test_flipkart_payloads_never_carry_the_amazon_identifier_names() -> None:
    names = (c.EXTERNAL_ID_ATTRIBUTE, c.EXEMPTION_ATTRIBUTE)
    for sent in (fk_item(), fk_family()):
        barcoded = {u.product_id: SELLER_FACTS for u in sent.units()}
        for facts in (barcoded, dict.fromkeys(barcoded, BARE)):
            items, errors = fk_mapping.build(dataclasses.replace(sent, unit_facts=facts))
            text = json.dumps([i.payload for i in items], default=str)
            assert not any(n in text for n in names)
            assert _ids(errors) == []


_FACTS = st.builds(
    lambda t, v, exempt: dataclasses.replace(
        SELLER_FACTS, identifier_type=t, identifier_value=v, identifier_exempt=exempt
    ),
    st.sampled_from([None, "ean", "upc", "gtin", "isbn", ""]),
    st.sampled_from([None, "", " ", EAN, "012345678905"]),
    st.booleans(),
)


@settings(max_examples=150, deadline=None)
@given(facts=st.lists(_FACTS, min_size=5, max_size=5), require=st.booleans())
def test_every_combination_sends_at_most_one_shape_and_refuses_only_bare_creates(
    facts: list[SellerFacts], require: bool
) -> None:
    sent = amazon_family()
    units = sent.units()
    by_id = {u.product_id: facts[i % len(facts)] for i, u in enumerate(units)}
    items, errors = mapping.build(
        dataclasses.replace(sent, unit_facts=by_id), require_identifier=require
    )
    refused = {e.field for e in _ids(errors)}
    for item in items:
        attrs = item.payload["attributes"]
        has_id, has_ex = c.EXTERNAL_ID_ATTRIBUTE in attrs, c.EXEMPTION_ATTRIBUTE in attrs
        assert not (has_id and has_ex)
        unit = item.unit
        if unit.item_role == "parent":
            assert not (has_id or has_ex)
            continue
        f = by_id[unit.product_id]
        good = f.identifier_type in c.IDENTIFIER_TYPES and bool((f.identifier_value or "").strip())
        assert has_id == good
        assert has_ex == (not good and f.identifier_exempt)
        path = mapping.fact_path(unit, "identifier")
        assert (path in refused) == (require and not good and not f.identifier_exempt)
        if has_id:
            (entry,) = attrs[c.EXTERNAL_ID_ATTRIBUTE]
            assert entry["value"] == f.identifier_value
