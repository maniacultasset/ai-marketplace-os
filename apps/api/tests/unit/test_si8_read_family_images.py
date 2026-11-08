"""SI8 fix round 1 (R1-1) re-check: a family read never files an image issue on a child's
wrong `images[k]`, whatever slot or casing Amazon sends back."""

from __future__ import annotations

from typing import Any

import pytest
from tests.adapters.amazon_support import SELLER, amazon_adapter, colour_family
from tests.support.fake_marketplace import build_fake

from .test_amazon_read_states import _row


def _issue(name: str, severity: str = "ERROR") -> dict[str, Any]:
    return {
        "code": "90220",
        "message": "Bad image.",
        "severity": severity,
        "attributeNames": [name],
    }


async def _family() -> tuple[Any, Any, Any]:
    app = build_fake()
    adapter = amazon_adapter(app, rows=[_row()])
    sent, _at = colour_family()
    await adapter.create_listing(sent)
    return app, adapter, sent


@pytest.mark.parametrize("severity", ["ERROR", "WARNING"])
@pytest.mark.parametrize(
    "name",
    [
        "main_product_image_locator",
        "other_product_image_locator_1",
        "other_product_image_locator_8",
    ],
)
async def test_every_child_image_issue_lands_on_listing(name: str, severity: str) -> None:
    app, adapter, sent = await _family()
    for child in [u for u in sent.units() if u.item_role == "child"]:
        app.state.fake.spapi_items[(SELLER, child.seller_sku)]["issues"] = [_issue(name, severity)]
        got = await adapter.get_listing(child.seller_sku, listing_id=sent.listing_id)
        assert [e.field for e in got.errors] == ["listing"], child.seller_sku
        assert not [f for f in got.fields if f.field.startswith("images[")]


async def test_child_detected_by_parentage_alone_any_case() -> None:
    """Amazon may echo `parentage_level` without the relationship, or in another case."""
    app, adapter, sent = await _family()
    child = next(u for u in sent.units() if u.item_role == "child")
    stored = app.state.fake.spapi_items[(SELLER, child.seller_sku)]
    stored["attributes"].pop("child_parent_sku_relationship")
    stored["attributes"]["parentage_level"] = [
        {"value": "CHILD", "marketplace_id": "A21TJRUUN4KGV"}
    ]
    stored["issues"] = [_issue("other_product_image_locator_1")]
    got = await adapter.get_listing(child.seller_sku, listing_id=sent.listing_id)
    assert [e.field for e in got.errors] == ["listing"]
