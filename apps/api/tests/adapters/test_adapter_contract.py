"""S14b T10 (AC 6; Done-when 2): `FakeAdapter` passes the adapter contract suite."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass as dataclasses_dataclass

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


FLIPKART_FIXTURES = (
    "create_ok",
    "create_partial",
    "update_ok",
    "get_active",
    "error_429",
    "error_401",
)


@pytest.mark.parametrize("name", FLIPKART_FIXTURES)
async def test_flipkart_fixture_replay(name: str) -> None:
    """S22 T14 (D6): each believed request/response pair replayed through the adapter over a
    MockTransport: the request the adapter builds matches the fixture's method, path and listing
    keys, and the adapter reads the recorded answer as the fixture expects. The stateful legs of
    the contract suite run against the fake (`test_adapter_contract_flipkart`)."""
    import dataclasses

    from app.adapters import base
    from tests.adapters.flipkart_support import fk_item, flipkart_adapter, listing
    from tests.support.fake_marketplace import build_fake
    from tests.support.fixture_transport import fixture_transport, load_fixture

    fixture = load_fixture("flipkart", name)
    transport = fixture_transport(fixture, base_path="/flipkart/sellers")
    app = build_fake()  # only the token source uses it; every call goes to the fixture
    adapter = flipkart_adapter(app, transport=transport, sleep=_no_sleep)
    sent = fk_item(listing(), sku="FK-FIX-1")
    expect = fixture["expect"]

    async def run() -> object:
        if fixture["operation"] == "create":
            return await adapter.create_listing(sent)
        if fixture["operation"] == "update":
            return await adapter.update_listing(sent, fields=["content.title"])
        return await adapter.get_listing("FK-FIX-1")

    if "raises" in expect:
        with pytest.raises(getattr(base, expect["raises"])):
            await run()
        return
    result = await run()
    got = dataclasses.asdict(_View.of(result))
    assert got["status"] == expect["status"], got
    if "failed" in expect:
        assert got["failed"] == expect["failed"], got
    if "marketplace_state" in expect:
        assert got["marketplace_state"] == expect["marketplace_state"], got
    assert transport.seen  # type: ignore[attr-defined]


async def _no_sleep(delay: float) -> None:
    return None


@dataclasses_dataclass
class _View:
    status: str
    failed: list[str]
    marketplace_state: str | None

    @classmethod
    def of(cls, result: object) -> _View:
        from mkos_schemas.publish import ListingResult

        assert isinstance(result, ListingResult)
        state = result.marketplace_state.value if result.marketplace_state else None
        failed = sorted(f.field for f in result.fields if f.status == "failed")
        return cls(result.status, failed, state)


def test_flipkart_fixtures_provenance() -> None:
    """Every Flipkart fixture says where it came from; a recorded one says when."""
    import json
    from pathlib import Path

    folder = Path(__file__).resolve().parents[1] / "fixtures" / "flipkart"
    files = sorted(folder.glob("*.json"))
    assert {f.stem for f in files} == set(FLIPKART_FIXTURES)
    for path in files:
        data = json.loads(path.read_text("utf-8"))
        assert data["provenance"] in ("believed", "recorded"), path.name
        if data["provenance"] == "recorded":
            assert data.get("recorded_at"), path.name
        assert {"request", "response", "operation", "expect"} <= set(data), path.name
