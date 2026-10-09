"""S21 T19 (AC11; CP14; DATABASE.md:890): the status sync's marketplace handle reads only."""

from __future__ import annotations

import pytest
from app.adapters.base import RateLimited
from app.adapters.fake import FakeAdapter
from app.workers.readers import ReaderView, ReadFailed
from mkos_schemas.errors import ErrorReport


def test_sync_uses_reader_only() -> None:
    view = ReaderView(FakeAdapter("amazon"))
    public = {name for name in dir(view) if not name.startswith("_")}
    assert public == {"get_listing", "aclose"}
    for name in ("create_listing", "update_listing", "publish_listing", "_adapter", "__getattr__"):
        assert not hasattr(type(view), name), name
        with pytest.raises(AttributeError):
            getattr(view, name)
    with pytest.raises(AttributeError):
        view.extra = 1  # type: ignore[attr-defined]


async def test_reader_delegates_and_translates() -> None:
    fake = FakeAdapter("amazon")
    view = ReaderView(fake)
    assert await view.get_listing("NOPE") is None

    async def throttled(*a: object, **k: object) -> object:
        raise RateLimited(ErrorReport(status="failed"))

    fake.get_listing = throttled  # type: ignore[method-assign,assignment]
    with pytest.raises(ReadFailed):
        await view.get_listing("ANY")
