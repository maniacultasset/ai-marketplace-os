"""SI2 T13 (AC12): the submission snapshot carries the seller facts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from decimal import Decimal

from app.services.submission_store import snapshot_bytes
from tests.adapters.amazon_support import SELLER_FACTS, amazon_family, amazon_item


def _snap(item: object) -> bytes:
    return snapshot_bytes(item, operation="create", field_paths=None, image_tags={})  # type: ignore[arg-type]


def test_child_price_change_moves_the_sha() -> None:
    family = amazon_family()
    child = family.units()[1]
    facts = dict(family.unit_facts)
    facts[child.product_id] = dataclasses.replace(SELLER_FACTS, selling_price=Decimal("450.00"))
    changed = dataclasses.replace(family, unit_facts=facts)
    assert hashlib.sha256(_snap(family)).digest() != hashlib.sha256(_snap(changed)).digest()


def test_same_input_same_bytes_and_exact_decimals() -> None:
    sent = amazon_item()
    one = dataclasses.replace(sent, unit_facts={sent.listing.product_id: dataclasses.replace(SELLER_FACTS, mrp=Decimal("1.10"), selling_price=Decimal("1.10"))})  # fmt: skip
    assert _snap(one) == _snap(one)
    body = json.loads(_snap(one))
    (facts,) = body["facts"].values()
    assert facts["mrp"] == "1.10" and facts["selling_price"] == "1.10"
    assert facts["fiber_composition"] == "100% Cotton"


def test_no_url_in_the_snapshot() -> None:
    assert b"https://" not in _snap(amazon_item(image_refs={}))
