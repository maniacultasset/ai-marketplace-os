"""ListingResult and publish outcomes."""

from __future__ import annotations

from uuid import UUID

import pytest
from mkos_schemas.enums import ItemMarketplaceState, Marketplace, MarketplaceState
from mkos_schemas.publish import FieldResult, ItemResult, ListingResult
from pydantic import ValidationError

_LID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _listing(**overrides: object) -> ListingResult:
    base = {
        "listing_id": _LID,
        "marketplace": Marketplace.AMAZON,
        "status": "published",
        "idempotency_key": "key-1",
    }
    base.update(overrides)
    return ListingResult.model_validate(base)


def test_listing_result_status_vocabulary() -> None:
    for st in ("published", "partially_published", "failed"):
        _listing(status=st)
    with pytest.raises(ValidationError):
        _listing(status="draft")


def test_field_result_field_is_legal_path() -> None:
    FieldResult.model_validate({"field": "content.title", "status": "succeeded"})
    with pytest.raises(ValidationError):
        FieldResult.model_validate({"field": "title", "status": "succeeded"})


def test_listing_result_rev3_fields() -> None:
    obj = _listing(
        submission_ref="sub-1",
        marketplace_state=MarketplaceState.LIVE,
        items=[
            {
                "product_id": _PID,
                "seller_sku": "SKU1",
                "marketplace_state": ItemMarketplaceState.LIVE,
            }
        ],
    )
    assert obj.submission_ref == "sub-1"
    assert obj.marketplace_state is MarketplaceState.LIVE
    assert obj.items[0].seller_sku == "SKU1"


def test_item_result_uses_item_marketplace_state() -> None:
    with pytest.raises(ValidationError):
        ItemResult.model_validate(
            {
                "product_id": _PID,
                "seller_sku": "s",
                "marketplace_state": MarketplaceState.PARTIALLY_LIVE,
            }
        )
    _listing(marketplace_state=MarketplaceState.PARTIALLY_LIVE)


def test_listing_result_idempotency_key_required() -> None:
    with pytest.raises(ValidationError):
        ListingResult.model_validate(
            {"listing_id": _LID, "marketplace": "amazon", "status": "published"}
        )
    with pytest.raises(ValidationError):
        _listing(idempotency_key="  ")
