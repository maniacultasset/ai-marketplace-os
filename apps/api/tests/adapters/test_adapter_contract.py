"""S14b T10 (AC 6; Done-when 2): `FakeAdapter` passes the adapter contract suite."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from tests.adapters.contract import run_contract_suite


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
async def test_adapter_contract_fake(marketplace: str) -> None:
    from app.adapters.fake import FakeAdapter

    def factory(fail: Mapping[str, tuple[str, str]]) -> FakeAdapter:
        return FakeAdapter(marketplace, fail_fields=fail)

    assert await run_contract_suite(factory) == []


async def test_adapter_contract_catches_a_bool() -> None:
    """The suite is not vacuous: an adapter that answers `True` is reported."""
    from app.adapters.base import AdapterListing
    from app.adapters.fake import FakeAdapter

    class Lying(FakeAdapter):
        async def create_listing(self, item: AdapterListing) -> bool:  # type: ignore[override]
            return True

    failures = await run_contract_suite(lambda fail: Lying("amazon", fail_fields=fail))
    assert any(f.startswith("returns_listing_result") for f in failures), failures


async def test_adapter_contract_amazon() -> None:
    """S20 T12 (Done-when 1): the Amazon adapter passes the same suite, against the fake SP-API
    over ASGI (no network)."""
    from tests.adapters.amazon_support import amazon_adapter, prepare, reject
    from tests.support.fake_marketplace import build_fake

    def factory(fail: Mapping[str, tuple[str, str]]) -> object:
        app = build_fake()
        reject(app, fail)
        return amazon_adapter(app)

    assert await run_contract_suite(factory, prepare=prepare) == []  # type: ignore[arg-type]


async def test_adapter_contract_flipkart() -> None:
    """S22 T14 (Done-when 1): the Flipkart adapter passes the same suite, against the fake
    Flipkart listings API over ASGI (no network). `prepare` is what the shared input builder
    gives Flipkart: no parent row, each child's colourway key."""
    from tests.adapters.flipkart_support import flipkart_adapter, prepare, reject
    from tests.support.fake_marketplace import build_fake

    def factory(fail: Mapping[str, tuple[str, str]]) -> object:
        app = build_fake()
        reject(app, fail)
        return flipkart_adapter(app)

    assert await run_contract_suite(factory, prepare=prepare) == []  # type: ignore[arg-type]
