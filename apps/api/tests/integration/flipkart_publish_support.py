"""The S22b publish test world: S21's publish world (`pub`) plus an active Flipkart account per
tenant and the real `FlipkartAdapter` over the in-process fake Flipkart listings API (no network).

The publish job's adapter factory hands a Flipkart account the `FlipkartAdapter` built with the
job's raw-body sink (S21 CP12) and the tenant's enabled Flipkart error-map rows, and an Amazon
account S21's `FakeAdapter`. The status sync's reader gets the same adapter. `flag()` switches
`FLIPKART_ENABLED` for the API and the worker of the test process alike.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from .publish_support import PubWorld, insert_account, pub

__all__ = ["pub"]
FK_API = "http://127.0.0.1:4020/flipkart/sellers"


def flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("FLIPKART_ENABLED", "true" if on else "false")
    get_settings.cache_clear()


@dataclass
class FkWorld:
    pub: PubWorld
    app: Any
    accounts: dict[str, uuid.UUID] = field(default_factory=dict)
    opened: list[uuid.UUID] = field(default_factory=list)

    @property
    def world(self) -> Any:
        return self.pub.world

    def calls(self) -> list[dict[str, Any]]:
        return [c for c in self.app.state.fake.calls if c["marketplace"] == "flipkart"]

    async def control(self, **body: Any) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(self.app), base_url="http://fake.test"
        ) as client:
            response = await client.post("/__control", json=body)
            assert response.status_code < 300, response.text

    def jobs(self, listing_id: uuid.UUID, kind: str = "listing_publish") -> int:
        return int(
            self.pub.scalar(
                "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = %s",
                [listing_id, kind],
            )
        )


async def _rows(tenant_id: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant_id) as uow:
        return await uow.error_map.enabled_rows("flipkart")


@pytest.fixture
async def fkp(pub: PubWorld, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FkWorld]:
    from app.adapters.base import MarketplaceDisabled
    from app.adapters.error_map import ErrorMapper
    from app.adapters.flipkart.adapter import FlipkartAdapter
    from app.core.config import get_settings
    from app.core.flags import marketplace_enabled, not_enabled_report
    from app.workers import publish as publish_worker
    from app.workers import readers
    from tests.adapters.flipkart_support import flipkart_client
    from tests.support.fake_marketplace import build_fake

    flag(monkeypatch, True)
    fk = FkWorld(pub, build_fake())
    for slug in ("acme", "bharat"):
        fk.accounts[slug] = insert_account(
            pub.world, slug, f"FK{uuid.uuid4().hex[:8].upper()}", marketplace="flipkart"
        )
    flipkart_ids = set(fk.accounts.values())

    async def adapter(tenant_id: uuid.UUID, account_id: uuid.UUID, sink: Any) -> Any:
        if account_id not in flipkart_ids:
            pub.sinks.append(sink)
            return pub.fake
        # open_adapter's flag gate (S22 T13), so the job sees what production sees
        if not marketplace_enabled("flipkart", get_settings()):
            raise MarketplaceDisabled(not_enabled_report("flipkart"))
        fk.opened.append(account_id)
        client = flipkart_client(fk.app, api_base=FK_API)
        return FlipkartAdapter(client, ErrorMapper(await _rows(tenant_id)), sink)

    async def reader(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
        from app.adapters.base import NullSink

        return await adapter(tenant_id, account_id, NullSink())

    publish_worker.set_adapter_factory(adapter)
    readers.set_reader_factory(reader)
    yield fk
