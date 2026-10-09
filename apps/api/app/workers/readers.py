"""`open_reader`: the read-only marketplace handle of the status sync (S21 T19; CP14; AC11;
DATABASE.md:890; invariant 1).

`ReaderView` wraps an adapter and exposes exactly one method, `get_listing`: no `create_listing`,
`update_listing` or `publish_listing`, no `__getattr__`, and the wrapped adapter sits in a
name-mangled slot. A SKU the marketplace does not know reads as None; a failed read raises
`ReadFailed` (the job's transport retry), so the sync never needs the adapter module's types. The sync job and service never import the adapter or `open_adapter` themselves
(`.importlinter` `status-sync-reader-only`); they get a `ReaderView` from here.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from mkos_schemas.publish import ListingResult

from app.adapters.base import AdapterError, ListingNotFound, MarketplaceAdapter, NullSink

ReaderFactory = Callable[[uuid.UUID, uuid.UUID], Awaitable[MarketplaceAdapter]]


class ReadFailed(Exception):  # noqa: N818 - a transport signal to the sync job
    """The marketplace read failed (5xx, throttled, auth): retry the sync job."""


class ReaderView:
    __slots__ = ("__adapter",)

    def __init__(self, adapter: MarketplaceAdapter) -> None:
        self.__adapter = adapter

    def __repr__(self) -> str:
        return "ReaderView()"

    async def get_listing(
        self, seller_sku: str, *, listing_id: uuid.UUID | None = None
    ) -> ListingResult | None:
        try:
            return await self.__adapter.get_listing(seller_sku, listing_id=listing_id)
        except ListingNotFound:
            return None
        except AdapterError as exc:
            raise ReadFailed(type(exc).__name__) from None

    async def aclose(self) -> None:
        close = getattr(self.__adapter, "aclose", None)
        if close is not None:
            await close()


async def _default(tenant_id: uuid.UUID, account_id: uuid.UUID) -> MarketplaceAdapter:
    from app.workers.adapters import open_adapter

    return await open_adapter(tenant_id, account_id, sink=NullSink())


_factory: ReaderFactory = _default


def set_reader_factory(factory: ReaderFactory | None) -> None:
    """Tests install an in-process adapter; None restores `open_adapter`."""
    global _factory
    _factory = factory or _default


async def open_reader(tenant_id: uuid.UUID, account_id: uuid.UUID) -> ReaderView:
    return ReaderView(await _factory(tenant_id, account_id))
