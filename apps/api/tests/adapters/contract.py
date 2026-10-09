"""The adapter contract suite (spec L5253, L10529-10533; S14b T10). Every adapter, the fake now
and the real ones in S20-S22, must return `[]` from `run_contract_suite(factory)`.

`factory(fail_fields)` builds a fresh adapter whose submissions fail exactly the named fields
with the given `(raw_code, raw_text)`. The suite never needs a network for the fake; a real
adapter runs it against its fake marketplace server. `prepare(item)` (S20) lets a real adapter's
test add what S21's input builder adds for it (the pinned template); the fake needs none.

S20 legs: `preview()` opens no socket and answers one item per item; an update names seller SKUs
and touches only the named paths of those SKUs; a family is one call with one `ItemResult` per
item; a replayed key keeps the per-field outcome and stores one listing (`submission_ref` may
differ: a real marketplace answers each call with its own submission id).
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from app.adapters.base import (
    AdapterItem,
    AdapterListing,
    ListingNotFound,
    MarketplaceAdapter,
    submitted_fields,
)
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.publish import ListingResult

Factory = Callable[[Mapping[str, tuple[str, str]]], MarketplaceAdapter]
Prepare = Callable[[AdapterListing], AdapterListing]
FAMILY_CHILDREN = (("S", "navy blue"), ("M", "navy blue"))
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


def family(listing: CanonicalListing | None = None, *, key: str | None = None) -> AdapterListing:
    """A size x colour family: the parent and one child per `FAMILY_CHILDREN` row."""
    body = listing or sample_listing()
    parent_sku = f"FAM-{uuid.uuid4().hex[:8]}"
    parent = AdapterItem(body.product_id, parent_sku, "parent")
    children = tuple(
        AdapterItem(uuid.uuid4(), f"{parent_sku}-{size}", "child", {"size": size, "color": color})
        for size, color in FAMILY_CHILDREN
    )
    return AdapterListing(
        listing_id=body.listing_id or uuid.uuid4(),
        seller_sku=parent_sku,
        idempotency_key=key or uuid.uuid4().hex,
        listing=body,
        items=(parent, *children),
        variation_theme="size_color",
    )


_build_item, _build_family = item, family


@contextmanager
def no_sockets() -> Iterator[None]:
    """Any socket opened inside the block fails the leg."""
    real = socket.socket

    class _Blocked(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("opened a socket")

    socket.socket = _Blocked  # type: ignore[misc]
    try:
        yield
    finally:
        socket.socket = real  # type: ignore[misc]


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


async def run_contract_suite(factory: Factory, *, prepare: Prepare | None = None) -> list[str]:
    failures: list[str] = []

    def item(  # the suite's items go through `prepare`
        listing: CanonicalListing | None = None, *, sku: str | None = None, key: str | None = None
    ) -> AdapterListing:
        built = _build_item(listing, sku=sku, key=key)
        return prepare(built) if prepare else built

    def family(
        listing: CanonicalListing | None = None, *, key: str | None = None
    ) -> AdapterListing:
        built = _build_family(listing, key=key)
        return prepare(built) if prepare else built

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

    async def rejected_create_not_found() -> None:
        sent = item()
        everything = dict.fromkeys(submitted_fields(sent.listing), ("X1", "Rejected."))
        adapter = factory(everything)
        await adapter.create_listing(sent)
        try:
            await adapter.get_listing(sent.seller_sku)
        except ListingNotFound:
            return
        raise AssertionError("get_listing found a listing whose create was fully rejected")

    async def partial_create_reads_partial() -> None:
        adapter = factory(title_fail)
        sent = item()
        await adapter.create_listing(sent)
        got = _is_result(await adapter.get_listing(sent.seller_sku), "get_listing")
        assert got.status == "partially_published", got.status
        assert got.marketplace_state == "partially_live", got.marketplace_state

    async def replay_is_equal() -> None:
        adapter = factory({})
        sent = item(key="replay-key")
        first = await adapter.create_listing(sent)
        changed = sample_listing(content=sent.listing.content.model_copy(update={"title": "Other"}))
        again = await adapter.create_listing(item(changed, sku=sent.seller_sku, key="replay-key"))
        assert again.status == first.status, "a replayed key changed the status"
        outcome = [(f.field, f.status) for f in again.fields]
        assert outcome == [(f.field, f.status) for f in first.fields], "outcome changed"
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

    async def preview_no_io() -> None:
        adapter = factory({})
        for sent in (item(), family()):
            with no_sockets():
                preview = adapter.preview(sent)
            got = [(p.seller_sku, p.item_role) for p in preview.items]
            assert got == [(u.seller_sku, u.item_role) for u in sent.units()], got

    async def family_is_one_result() -> None:
        adapter = factory({})
        sent = family()
        result = _is_result(await adapter.create_listing(sent), "create_listing")
        assert result.status == "published", result.status
        got = [(i.seller_sku, i.product_id) for i in result.items]
        assert got == [(u.seller_sku, u.product_id) for u in sent.units()], got

    async def update_named_skus_only() -> None:
        adapter = factory({})
        sent = family()
        await adapter.create_listing(sent)
        changed = sample_listing(
            content=sent.listing.content.model_copy(update={"title": "Child title"})
        )
        first_child, second_child = sent.units()[1:3]
        update = AdapterListing(
            listing_id=sent.listing_id,
            seller_sku=sent.seller_sku,
            idempotency_key=uuid.uuid4().hex,
            listing=changed,
            items=sent.items,
            variation_theme=sent.variation_theme,
            template=sent.template,
        )
        result = await adapter.update_listing(
            update, fields={second_child.seller_sku: ["content.title"]}
        )
        assert [i.seller_sku for i in result.items] == [second_child.seller_sku], result.items
        assert [f.field for f in result.fields] == ["content.title"], result.fields
        stored = getattr(adapter, "stored", None)
        if stored is not None:
            assert stored(second_child.seller_sku)["content.title"] == "Child title", "not written"
            untouched = stored(first_child.seller_sku)["content.title"]
            assert untouched == sent.listing.content.title, "another SKU was overwritten"

    for name, probe in (
        ("returns_listing_result", returns_results),
        ("one_field_result_per_field", one_result_per_field),
        ("partial_keeps_raw_code_and_text", partial_keeps_raw),
        ("all_failed_is_failed", all_failed_is_failed),
        ("rejected_create_is_not_found", rejected_create_not_found),
        ("partial_create_reads_back_partial", partial_create_reads_partial),
        ("replayed_key_is_equal", replay_is_equal),
        ("update_touches_only_given_fields", update_only_given),
        ("unknown_sku_raises", unknown_sku_raises),
        ("performance_range_checked", bad_range_raises),
        ("preview_opens_no_socket", preview_no_io),
        ("family_is_one_result", family_is_one_result),
        ("update_touches_only_named_skus", update_named_skus_only),
    ):
        await _check(name, failures, probe)
    return failures
