"""S26 T10/T12 (AC 2 API10, AC 5): the publish job through the real `AmazonAdapter` and SP-API
client, against the in-process fake marketplace (tests/support/fake_marketplace.py, over
`httpx.ASGITransport`: no network). The S21 publish world (`pub`) with its factory swapped.

`AmzWorld.control(**switches)` drives the fake's `/__control`; `writes()` counts the PUT and
PATCH calls the fake saw (the "no duplicate marketplace write" check); `sleeps` holds the
client's backoff waits (never slept for real).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from .publish_support import PubWorld, insert_account

__all__ = ["AmzWorld", "amz"]
# The one error-map row these suites need: a transient per-attribute code the publish job may
# retry (a retryable condition), so a partial publish can be re-sent field by field.
RETRYABLE_CODE = "S26RETRY"


def _error_map() -> tuple[Any, ...]:
    from app.adapters.error_map import ErrorMapRow
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorClass, RetryClass, Severity

    return (
        ErrorMapRow(
            "amazon", "code", RETRYABLE_CODE, 1, ErrorCode.RATE_LIMITED,
            ErrorClass.MARKETPLACE_TECHNICAL, RetryClass.AUTO_RETRY, Severity.ERROR, False,
            None, None, "{field} was throttled on {marketplace}.", None,
        ),
    )  # fmt: skip


ERROR_MAP: tuple[Any, ...] = ()


@dataclass
class AmzWorld:
    pub: PubWorld
    app: Any
    transport: Any = None
    sleeps: list[float] = field(default_factory=list)
    tokens: Any = None

    @property
    def world(self) -> Any:
        return self.pub.world

    @property
    def state(self) -> Any:
        return self.app.state.fake

    async def control(self, **body: Any) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(self.app), base_url="http://fake.test"
        ) as client:
            response = await client.post("/__control", json=body)
            assert response.status_code == 200, response.text

    def writes(self, sku_prefix: str = "") -> int:
        return sum(
            1
            for call in self.state.calls
            if call["method"] in ("PUT", "PATCH") and sku_prefix in call["path"]
        )

    def reads(self) -> int:
        return sum(1 for call in self.state.calls if call["method"] == "GET")


@pytest.fixture
async def amz(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AmzWorld]:
    from app.adapters.amazon.adapter import AmazonAdapter
    from app.adapters.amazon.client import SpApiClient
    from app.adapters.error_map import ErrorMapper
    from app.db.queue import get_queue_app
    from app.workers import publish as publish_worker
    from app.workers import readers
    from tests.adapters.amazon_support import CountingLimiter, FakeTokens, FixedRng
    from tests.support.fake_marketplace import build_fake

    from .generation_support import build_gen_world

    global ERROR_MAP
    ERROR_MAP = _error_map()
    get_queue_app.cache_clear()
    world = await build_gen_world(auth_db, monkeypatch)
    app = build_fake()
    pw = PubWorld(world, None)
    for slug in ("acme", "bharat"):
        pw.accounts[slug] = insert_account(world, slug)
    aw = AmzWorld(pw, app)
    tokens = FakeTokens(app)

    def client() -> Any:
        async def sleep(delay: float) -> None:
            aw.sleeps.append(delay)

        return SpApiClient(
            endpoint="http://fake.test",
            marketplace_id="A21TJRUUN4KGV",
            seller_id="A1S26SELLER",
            tokens=tokens,
            limiter=CountingLimiter(),
            transport=aw.transport or httpx.ASGITransport(app),
            sleep=sleep,
            rng=FixedRng(0.0),
        )

    async def factory(tenant_id: uuid.UUID, account_id: uuid.UUID, sink: Any) -> Any:
        return AmazonAdapter(client(), ErrorMapper(ERROR_MAP), sink)

    async def reader(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
        return AmazonAdapter(client(), ErrorMapper(ERROR_MAP))

    publish_worker.set_adapter_factory(factory)
    readers.set_reader_factory(reader)
    aw.tokens = tokens
    try:
        yield aw
    finally:
        publish_worker.set_adapter_factory(None)
        readers.set_reader_factory(None)
        get_queue_app.cache_clear()
