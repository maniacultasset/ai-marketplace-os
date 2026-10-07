"""S14b tester round 2: attacks on the fake adapter's rejected and partial create read-back."""

from __future__ import annotations

import pytest
from tests.adapters.contract import item, sample_listing


async def test_rejected_publish_is_not_found() -> None:
    from app.adapters.base import ListingNotFound, submitted_fields
    from app.adapters.fake import FakeAdapter

    listing = sample_listing()
    adapter = FakeAdapter(
        "flipkart", fail_fields=dict.fromkeys(submitted_fields(listing), ("E", "x"))
    )
    result = await adapter.publish_listing(item(listing, sku="SKU-PUB-REJ"))
    assert result.status == "failed"
    assert adapter.creates == 0
    with pytest.raises(ListingNotFound):
        await adapter.get_listing("SKU-PUB-REJ")
    with pytest.raises(ListingNotFound):
        adapter.stored("SKU-PUB-REJ")


async def test_rejected_create_replay_still_not_found() -> None:
    from app.adapters.base import ListingNotFound, submitted_fields
    from app.adapters.fake import FakeAdapter

    listing = sample_listing()
    adapter = FakeAdapter(
        "amazon", fail_fields=dict.fromkeys(submitted_fields(listing), ("E", "x"))
    )
    sent = item(listing, sku="SKU-REPLAY", key="k-replay")
    first = await adapter.create_listing(sent)
    adapter._fail.clear()  # the marketplace would now accept, but the same key must replay
    again = await adapter.create_listing(sent)
    assert again == first and again.status == "failed"
    with pytest.raises(ListingNotFound):
        await adapter.get_listing("SKU-REPLAY")


async def test_retry_of_failed_field_reads_back_published() -> None:
    from app.adapters.fake import FakeAdapter
    from mkos_schemas.enums import MarketplaceState

    adapter = FakeAdapter("amazon", fail_fields={"content.title": ("8541", "bad")})
    sent = item(sku="SKU-RETRY")
    await adapter.create_listing(sent)
    adapter._fail.clear()
    retry = await adapter.update_listing(
        item(sent.listing, sku="SKU-RETRY"), fields=["content.title"]
    )
    assert retry.status == "published"
    got = await adapter.get_listing("SKU-RETRY")
    assert (got.status, got.marketplace_state) == ("published", MarketplaceState.LIVE)
    assert "content.title" in [f.field for f in got.fields]
    assert adapter.creates == 1


async def test_rejected_then_accepted_create_counts_one() -> None:
    from app.adapters.base import submitted_fields
    from app.adapters.fake import FakeAdapter

    listing = sample_listing()
    adapter = FakeAdapter(
        "amazon", fail_fields=dict.fromkeys(submitted_fields(listing), ("E", "x"))
    )
    await adapter.create_listing(item(listing, sku="SKU-TWICE"))
    adapter._fail.clear()
    ok = await adapter.create_listing(item(listing, sku="SKU-TWICE"))
    assert ok.status == "published"
    assert adapter.creates == 1
    assert (await adapter.get_listing("SKU-TWICE")).status == "published"
