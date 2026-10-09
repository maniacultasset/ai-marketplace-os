"""S20 T13 (AC3; D3, D11; C5; G17): the Postgres outbound limiter per (marketplace, account,
operation), with an injected clock and sleep (deterministic, well under a second)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest

from .conftest import LOCAL_HOSTS, Conn, libpq_url

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


class FakeTime:
    """One clock shared by every limiter; `sleep(d)` moves it to `max(now, start + d)`."""

    def __init__(self, start: float) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        start = self.now
        self.sleeps.append(delay)
        await asyncio.sleep(0)
        self.now = max(self.now, start + delay)


@pytest.fixture
async def limiter_db(db_settings: Any) -> AsyncIterator[None]:
    from app.db.session import dispose_engine

    yield
    await dispose_engine()


def _limiter(account: uuid.UUID, fake: FakeTime, **over: Any) -> Any:
    from app.workers.outbound_limiter import PgOutboundLimiter

    return PgOutboundLimiter(
        "amazon",
        account,
        rates=over.pop("rates", {"put": 1, "patch": 1, "get": 1}),
        sleep=fake.sleep,
        clock=fake.clock,
        **over,
    )


def _start() -> float:
    # a window range no other test uses: a random hour in 2031
    return 1_930_000_000.0 + uuid.uuid4().int % 3_000_000


async def test_outbound_limiter_two_adapters_40_calls(limiter_db: None) -> None:
    from tests.adapters.amazon_support import amazon_adapter, amazon_item
    from tests.support.fake_marketplace import build_fake

    app = build_fake()
    account = uuid.uuid4()
    fake = FakeTime(_start())
    admitted: list[float] = []

    class Recording:
        def __init__(self) -> None:
            self.inner = _limiter(account, fake)

        async def acquire(self, operation: str) -> float:
            window = float(await self.inner.acquire(operation))
            admitted.append(window)
            return window

    first, second = (amazon_adapter(app, limiter=Recording()) for _ in range(2))
    sent = amazon_item()
    from app.adapters.base import ListingNotFound

    async def worker(adapter: Any) -> None:
        for _ in range(20):
            with contextlib.suppress(ListingNotFound):
                await adapter.get_listing(sent.seller_sku)

    await asyncio.gather(worker(first), worker(second))
    per_window = Counter(admitted)
    assert len(admitted) == 40 and max(per_window.values()) <= 1
    assert len(app.state.fake.calls) == 40
    assert all(0 < s <= 1.0 for s in fake.sleeps)


async def test_outbound_limiter_other_account_independent(limiter_db: None) -> None:
    fake = FakeTime(_start())
    a, b = _limiter(uuid.uuid4(), fake), _limiter(uuid.uuid4(), fake)
    assert await a.acquire("put") == await b.acquire("put")  # same window, both admitted
    assert fake.sleeps == []
    await a.acquire("get")  # another operation is another bucket
    assert fake.sleeps == []


async def test_outbound_limiter_bounded_waits_rate_limited(limiter_db: None) -> None:
    from app.adapters.base import RateLimited

    fake = FakeTime(_start())

    async def always_full(key: str, window: Any) -> int:
        return 99

    limiter = _limiter(uuid.uuid4(), fake, increment=always_full)
    with pytest.raises(RateLimited) as caught:
        await limiter.acquire("put")
    assert len(fake.sleeps) == 30
    assert caught.value.report.error_code == "RATE_LIMITED"


async def test_outbound_limiter_autocommit_outside_business_tx(
    limiter_db: None, superuser_conn: Conn
) -> None:
    """A transaction holding a lock on the same counter row on another connection does not
    block `acquire` past its own statement: the increment autocommits on its own connection."""
    import os

    fake = FakeTime(_start())
    account = uuid.uuid4()
    limiter = _limiter(account, fake, rates={"put": 5, "patch": 1, "get": 1})
    window = await limiter.acquire("put")
    from datetime import UTC, datetime

    from app.workers.outbound_limiter import bucket_key

    with psycopg.connect(libpq_url(os.environ["DATABASE_URL"])) as other:
        other.execute("BEGIN")
        other.execute("SELECT 1")  # an open business transaction, holding nothing we need
        await asyncio.wait_for(limiter.acquire("put"), timeout=5)
        other.execute("ROLLBACK")
    row = superuser_conn.execute(
        "SELECT n FROM rate_limit_counters WHERE bucket_key = %s AND window_start = %s",
        [bucket_key("amazon", account, "put"), datetime.fromtimestamp(window, UTC)],
    ).fetchone()
    assert row == (2,)


def test_outbound_limiter_bucket_key_shape() -> None:
    from app.workers.outbound_limiter import bucket_key

    account = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert bucket_key("amazon", account, "put") == f"mkt:amazon:{account}:put"


def test_outbound_limiter_unknown_operation_refused() -> None:
    from app.workers.outbound_limiter import PgOutboundLimiter

    with pytest.raises(ValueError, match="at least 1"):
        PgOutboundLimiter("amazon", uuid.uuid4(), rates={"put": 0})
    limiter = PgOutboundLimiter("amazon", uuid.uuid4(), rates={"put": 1})
    with pytest.raises(ValueError, match="unknown"):
        asyncio.run(limiter.acquire("delete"))
