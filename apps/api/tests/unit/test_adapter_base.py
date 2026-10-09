"""S20 T1 (AC1, AC5; D4, D5): the adapter framework types in `app/adapters/base.py`."""

from __future__ import annotations

import uuid

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport
from tests.adapters.contract import sample_listing


def _listing(**over: object):  # type: ignore[no-untyped-def]
    from app.adapters.base import AdapterListing

    body = sample_listing()
    fields: dict[str, object] = {
        "listing_id": uuid.uuid4(),
        "seller_sku": "SKU-1",
        "idempotency_key": "key-1",
        "listing": body,
    }
    fields.update(over)
    return AdapterListing(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("attempt", [0, 4, -1, True])
def test_adapter_listing_attempt_bounds_refused(attempt: object) -> None:
    with pytest.raises(ValueError, match="attempt"):
        _listing(attempt=attempt)


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_adapter_listing_attempt_bounds(attempt: int) -> None:
    assert _listing(attempt=attempt).attempt == attempt


def test_adapter_listing_items_roles() -> None:
    from app.adapters.base import AdapterItem

    with pytest.raises(ValueError, match="item_role"):
        AdapterItem(uuid.uuid4(), "SKU-1", "variant")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="seller_sku"):
        AdapterItem(uuid.uuid4(), "  ", "standalone")


def test_adapter_listing_family_shape() -> None:
    from app.adapters.base import AdapterItem

    parent = AdapterItem(uuid.uuid4(), "SKU-1", "parent")
    child = AdapterItem(uuid.uuid4(), "SKU-1-S", "child", {"size": "S"})
    family = _listing(items=(parent, child), variation_theme="size")
    assert family.units() == (parent, child)
    with pytest.raises(ValueError, match="parent"):
        _listing(items=(child, parent))  # the parent comes first
    with pytest.raises(ValueError, match="parent"):
        _listing(items=(AdapterItem(uuid.uuid4(), "OTHER", "parent"), child))
    with pytest.raises(ValueError, match="standalone"):
        _listing(items=(AdapterItem(uuid.uuid4(), "SKU-1", "standalone"), child))
    with pytest.raises(ValueError, match="duplicate"):
        _listing(items=(parent, child, child))


def test_adapter_listing_default_unit_is_standalone() -> None:
    sent = _listing()
    (unit,) = sent.units()
    assert (unit.seller_sku, unit.item_role, unit.product_id) == (
        "SKU-1",
        "standalone",
        sent.listing.product_id,
    )


def _report() -> ErrorReport:
    return ErrorReport(status="failed", error_code=ErrorCode.RATE_LIMITED, retryable=True)


def test_adapter_error_carries_report_and_completed() -> None:
    from app.adapters.base import AdapterError, MarketplaceAmbiguous, RateLimited
    from mkos_schemas.enums import ItemMarketplaceState
    from mkos_schemas.publish import ItemResult

    done = ItemResult(
        product_id=uuid.uuid4(),
        seller_sku="SKU-1",
        marketplace_state=ItemMarketplaceState.PROCESSING,
    )
    exc = MarketplaceAmbiguous(_report(), completed=(done,))
    assert isinstance(exc, AdapterError)
    assert exc.report.error_code is ErrorCode.RATE_LIMITED
    assert exc.completed == (done,)
    assert RateLimited(_report()).completed == ()


def test_adapter_error_str_has_no_report_text() -> None:
    from app.adapters.base import AuthExpired
    from mkos_schemas.enums import ErrorSource
    from mkos_schemas.errors import ErrorItem

    item = ErrorItem(
        field="listing",
        error_code=ErrorCode.AUTH_EXPIRED,
        message="token CANARYzq9-0011223344556677 rejected",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
    )
    exc = AuthExpired(ErrorReport(status="failed", errors=[item]))
    assert str(exc) == "AuthExpired"
    assert "CANARY" not in repr(exc)
    assert exc.args == ()


def test_adapter_abc_requires_preview() -> None:
    from app.adapters.base import MarketplaceAdapter

    assert "preview" in MarketplaceAdapter.__abstractmethods__

    class NoPreview(MarketplaceAdapter):
        async def get_listing(self, seller_sku, *, listing_id=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def create_listing(self, item):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def update_listing(self, item, *, fields):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def publish_listing(self, item):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def get_performance(self, listing_id, from_date, to_date):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    with pytest.raises(TypeError, match="preview"):
        NoPreview()  # type: ignore[abstract]


def test_fields_by_sku_normalises() -> None:
    from app.adapters.base import AdapterItem, fields_by_sku

    parent = AdapterItem(uuid.uuid4(), "SKU-1", "parent")
    child = AdapterItem(uuid.uuid4(), "SKU-1-S", "child", {"size": "S"})
    family = _listing(items=(parent, child), variation_theme="size")
    assert fields_by_sku(family, ["content.title", "content.title"]) == {"SKU-1": ["content.title"]}
    assert fields_by_sku(family, {"SKU-1-S": ["content.title", "attributes.size"]}) == {
        "SKU-1-S": ["attributes.size", "content.title"]
    }
    with pytest.raises(ValueError, match="unknown seller_sku"):
        fields_by_sku(family, {"NOPE": ["content.title"]})
    with pytest.raises(ValueError, match="legal field paths"):
        fields_by_sku(family, {"SKU-1": ["content.nope"]})
    with pytest.raises(ValueError, match="non-empty"):
        fields_by_sku(family, {"SKU-1": []})
    with pytest.raises(ValueError, match="non-empty"):
        fields_by_sku(family, {})
