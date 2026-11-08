"""`open_previewer`: the dry-run-only marketplace handle of the remote preview job (SI11; ADR-0028
proposed; invariant 1).

`PreviewView` wraps an adapter and exposes exactly one call, `preview_remote` (Amazon
`VALIDATION_PREVIEW`, which saves nothing): no create, update, publish or read, no
`__getattr__`, and the wrapped adapter sits in a name-mangled slot. The preview job and service
never import the adapter or `open_adapter` themselves (`.importlinter`
`remote-preview-preview-only`); they get a `PreviewView` from here, as the status sync gets a
`ReaderView` (`app/workers/readers.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from app.adapters.base import AdapterError, AdapterListing, NullSink, RemotePreview
from app.workers.readers import AccountInactive, ReadFailed

PreviewerFactory = Callable[[uuid.UUID, uuid.UUID], Awaitable[object]]


class PreviewUnsupported(Exception):  # noqa: N818 - a signal to the preview job
    """The account's adapter has no dry run (Flipkart): nothing is sent."""


class PreviewView:
    __slots__ = ("__adapter",)

    def __init__(self, adapter: object) -> None:
        if not callable(getattr(adapter, "preview_remote", None)):
            raise PreviewUnsupported(type(adapter).__name__)
        self.__adapter = adapter

    def __repr__(self) -> str:
        return "PreviewView()"

    async def preview_remote(self, item: AdapterListing) -> RemotePreview:
        try:
            result: RemotePreview = await self.__adapter.preview_remote(item)  # type: ignore[attr-defined]
        except AdapterError as exc:
            raise ReadFailed(type(exc).__name__) from None
        return result

    async def aclose(self) -> None:
        close = getattr(self.__adapter, "aclose", None)
        if close is not None:
            await close()


async def _default(tenant_id: uuid.UUID, account_id: uuid.UUID) -> object:
    from app.workers.adapters import open_adapter

    return await open_adapter(tenant_id, account_id, sink=NullSink())


_factory: PreviewerFactory = _default


def set_previewer_factory(factory: PreviewerFactory | None) -> None:
    """Tests install an in-process adapter; None restores `open_adapter`."""
    global _factory
    _factory = factory or _default


async def open_previewer(tenant_id: uuid.UUID, account_id: uuid.UUID) -> PreviewView:
    """The dry-run handle over the account's adapter. An account that refuses to open is
    `AccountInactive`; any other adapter failure there is `ReadFailed` (the job's retry)."""
    from app.adapters.base import AuthExpired

    try:
        adapter = await _factory(tenant_id, account_id)
    except AuthExpired:
        raise AccountInactive(str(account_id)) from None
    except AdapterError as exc:
        raise ReadFailed(type(exc).__name__) from None
    try:
        return PreviewView(adapter)
    except PreviewUnsupported:
        close = getattr(adapter, "aclose", None)
        if close is not None:
            await close()
        raise
