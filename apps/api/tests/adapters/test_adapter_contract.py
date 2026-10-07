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
