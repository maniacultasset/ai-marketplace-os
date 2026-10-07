"""S14b fix round 1 (R1-1): a fully rejected create stores nothing, and `get_listing` reads back
the status the last write left, so a partial create is never reported `published`/`live`."""

from __future__ import annotations

import pytest
from tests.adapters.contract import item, sample_listing


async def test_rejected_create_is_not_live() -> None:
    from app.adapters.base import ListingNotFound, submitted_fields
    from app.adapters.fake import FakeAdapter

    listing = sample_listing()
    adapter = FakeAdapter(
        "amazon", fail_fields=dict.fromkeys(submitted_fields(listing), ("E1", "bad"))
    )
    created = await adapter.create_listing(item(listing, sku="SKU-REJECTED"))
    assert created.status == "failed"
    assert adapter.creates == 0, "a fully rejected create stored a listing"
    with pytest.raises(ListingNotFound):
        await adapter.get_listing("SKU-REJECTED")
    with pytest.raises(ListingNotFound):
        await adapter.update_listing(item(listing, sku="SKU-REJECTED"), fields=["content.title"])


async def test_partial_create_reads_back_partial() -> None:
    from app.adapters.fake import FakeAdapter
    from mkos_schemas.enums import MarketplaceState

    adapter = FakeAdapter("amazon", fail_fields={"content.title": ("8541", "bad")})
    await adapter.create_listing(item(sku="SKU-PARTIAL"))
    got = await adapter.get_listing("SKU-PARTIAL")
    assert got.status == "partially_published"
    assert got.marketplace_state is MarketplaceState.PARTIALLY_LIVE
    assert "content.title" not in [f.field for f in got.fields]


async def test_all_failed_update_keeps_existing_listing() -> None:
    from app.adapters.fake import FakeAdapter
    from mkos_schemas.enums import MarketplaceState

    adapter = FakeAdapter("amazon")
    sent = item(sku="SKU-LIVE")
    await adapter.create_listing(sent)
    adapter._fail["content.title"] = ("E1", "bad")
    result = await adapter.update_listing(
        item(sent.listing, sku="SKU-LIVE"), fields=["content.title"]
    )
    assert result.status == "failed"
    got = await adapter.get_listing("SKU-LIVE")
    assert (got.status, got.marketplace_state) == ("published", MarketplaceState.LIVE)
    assert adapter.creates == 1
