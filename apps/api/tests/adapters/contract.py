"""The adapter contract suite (spec L5253, L10529-10533; S14b T10). Every adapter, the fake now
and the real ones in S20-S22, must return `[]` from `run_contract_suite(factory)`.

`factory(fail_fields)` builds a fresh adapter whose submissions fail exactly the named fields
with the given `(raw_code, raw_text)`. The suite never needs a network for the fake; a real
adapter runs it against its fake marketplace server.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from app.adapters.base import AdapterListing, ListingNotFound, MarketplaceAdapter, submitted_fields
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.publish import ListingResult

Factory = Callable[[Mapping[str, tuple[str, str]]], MarketplaceAdapter]
FIXTURE = Path(__file__).resolve().parents[4] / "packages/schemas/fixtures/CanonicalListing.json"


def sample_listing(**over: Any) -> CanonicalListing:
    listing = CanonicalListing.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    return listing.model_copy(update=over) if over else listing


def item(
    listing: CanonicalListing | None = None, *, sku: str | None = None, key: str | None = None
) -> AdapterListing:
    body = listing or sample_listing()
    return AdapterListing(
        listing_id=body.listing_id or uuid.uuid4(),
        seller_sku=sku or f"SKU-{uuid.uuid4().hex[:8]}",
        idempotency_key=key or uuid.uuid4().hex,
        listing=body,
    )


async def _check(name: str, failures: list[str], probe: Callable[[], Awaitable[None]]) -> None:
    try:
        await probe()
    except AssertionError as exc:
        failures.append(f"{name}: {exc}")
    except Exception as exc:  # a contract break, reported by name, never swallowed silently
        failures.append(f"{name}: raised {type(exc).__name__}")


def _is_result(value: object, where: str) -> ListingResult:
    assert isinstance(value, ListingResult), f"{where} returned {type(value).__name__}"
    return value


async def run_contract_suite(factory: Factory) -> list[str]:
    failures: list[str] = []
    title_fail = {"content.title": ("8541", "Title has a prohibited character.")}

    async def returns_results() -> None:
        adapter = factory({})
        first = item()
        _is_result(await adapter.create_listing(first), "create_listing")
        _is_result(await adapter.get_listing(first.seller_sku), "get_listing")
        update = item(first.listing, sku=first.seller_sku)
        _is_result(await adapter.update_listing(update, fields=["content.title"]), "update_listing")
        _is_result(await adapter.publish_listing(item()), "publish_listing")

    async def one_result_per_field() -> None:
        adapter = factory({})
        sent = item()
        result = _is_result(await adapter.create_listing(sent), "create_listing")
        assert [f.field for f in result.fields] == submitted_fields(sent.listing), "fields differ"
        assert result.status == "published", result.status

    async def partial_keeps_raw() -> None:
        adapter = factory(title_fail)
        result = _is_result(await adapter.create_listing(item()), "create_listing")
        assert result.status == "partially_published", result.status
        failed = [f for f in result.fields if f.status == "failed"]
        assert [f.field for f in failed] == ["content.title"], failed
        error = failed[0].error
        assert error is not None and error.raw_code == "8541", "raw code lost"
        assert error.message == "Title has a prohibited character.", "raw text lost"
        assert result.error is not None and result.error.status == "partial", "no partial report"

    async def all_failed_is_failed() -> None:
        sent = item()
        everything = dict.fromkeys(submitted_fields(sent.listing), ("X1", "Rejected."))
        result = _is_result(await factory(everything).create_listing(sent), "create_listing")
        assert result.status == "failed", result.status
        assert result.error is not None and result.error.status == "failed", "no failed report"

    async def replay_is_equal() -> None:
        adapter = factory({})
        sent = item(key="replay-key")
        first = await adapter.create_listing(sent)
        changed = sample_listing(content=sent.listing.content.model_copy(update={"title": "Other"}))
        again = await adapter.create_listing(item(changed, sku=sent.seller_sku, key="replay-key"))
        assert again == first, "a replayed key changed the result"
        creates = getattr(adapter, "creates", 1)
        assert creates == 1, f"{creates} stored creates"

    async def update_only_given() -> None:
        adapter = factory({})
        sent = item()
        await adapter.create_listing(sent)
        changed = sample_listing(
            content=sent.listing.content.model_copy(
                update={"title": "New title", "description": "New"}
            )
        )
        result = await adapter.update_listing(
            item(changed, sku=sent.seller_sku), fields=["content.title"]
        )
        assert [f.field for f in result.fields] == ["content.title"], result.fields
        stored = getattr(adapter, "stored", None)
        if stored is not None:
            values = stored(sent.seller_sku)
            assert values["content.title"] == "New title", "title not written"
            assert values["content.description"] == sent.listing.content.description, "overwrote"

    async def unknown_sku_raises() -> None:
        adapter = factory({})
        try:
            await adapter.get_listing("NO-SUCH-SKU")
        except ListingNotFound:
            pass
        else:
            raise AssertionError("get_listing on an unknown SKU did not raise")
        try:
            await adapter.update_listing(item(sku="NO-SUCH-SKU"), fields=["content.title"])
        except ListingNotFound:
            return
        raise AssertionError("update_listing on an unknown SKU did not raise")

    async def bad_range_raises() -> None:
        try:
            await factory({}).get_performance(uuid.uuid4(), date(2026, 1, 2), date(2026, 1, 1))
        except ValueError:
            return
        raise AssertionError("to < from did not raise ValueError")

    for name, probe in (
        ("returns_listing_result", returns_results),
        ("one_field_result_per_field", one_result_per_field),
        ("partial_keeps_raw_code_and_text", partial_keeps_raw),
        ("all_failed_is_failed", all_failed_is_failed),
        ("replayed_key_is_equal", replay_is_equal),
        ("update_touches_only_given_fields", update_only_given),
        ("unknown_sku_raises", unknown_sku_raises),
        ("performance_range_checked", bad_range_raises),
    ):
        await _check(name, failures, probe)
    return failures
