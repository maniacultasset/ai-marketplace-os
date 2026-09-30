"""AC6b against a real Postgres (fix round 1, F1; moved from the e2e tester's repro): `/readyz`
answers 503 within the readiness bound (P§7.4b) when the database stops answering on an
ESTABLISHED pooled connection, as in a network partition, and recovers once it answers again.

Stack: ASGI app -> ReadinessCheck -> app.db.session.ping -> SQLAlchemy pool -> psycopg -> a
loopback TCP proxy (own thread and event loop) -> the test database. In blackhole mode the proxy
keeps every socket open and silently drops every byte in both directions. psycopg then spends
~10 s on its cancel request and close after `ping`'s own timeout fires; that drain must never
delay a probe.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

if TYPE_CHECKING:
    from app.core.config import Settings

# The proxy listens on, and connects to, the loopback test database only.
pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

CHUNK = 65536
RECOVERY_DEADLINE_SECONDS = 30.0


class BlackholeProxy:
    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.blackhole = False
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stopping = asyncio.Event()
        self._writers: set[asyncio.StreamWriter] = set()
        self._thread = threading.Thread(target=self._run, name="blackhole-proxy", daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(5), "the proxy did not start"

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._stopping.set)
        self._thread.join(10)
        assert not self._thread.is_alive(), "the proxy did not stop"

    def _run(self) -> None:
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        await self._stopping.wait()
        server.close()
        for writer in self._writers:
            writer.close()
        await server.wait_closed()
        handlers = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in handlers:
            task.cancel()
        # Teardown: the handlers were just cancelled, so their CancelledErrors are expected.
        await asyncio.gather(*handlers, return_exceptions=True)

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        self._writers.add(client_w)
        if self.blackhole:  # a new connection (psycopg's cancel request): accept, never answer
            while await client_r.read(CHUNK):
                pass
            client_w.close()
            return
        upstream_r, upstream_w = await asyncio.open_connection(*self.upstream)
        self._writers.add(upstream_w)
        await asyncio.gather(self._pump(client_r, upstream_w), self._pump(upstream_r, client_w))

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(CHUNK):
            if not self.blackhole:
                writer.write(data)
                await writer.drain()
        writer.close()  # pass the EOF on


@pytest.fixture
async def proxy(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[BlackholeProxy]:
    """Points DATABASE_URL at the proxy in front of the test database (mos_test or mos_iso,
    checked by `db_settings`)."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    assert db_settings.database_url is not None
    parts = urlsplit(db_settings.database_url)
    started = BlackholeProxy(parts.hostname or "localhost", parts.port or 5432)
    started.start()
    try:
        await dispose_engine()
        netloc = f"{parts.username}:{parts.password}@127.0.0.1:{started.port}"
        monkeypatch.setenv("DATABASE_URL", urlunsplit(parts._replace(netloc=netloc)))
        get_settings.cache_clear()
        yield started
    finally:
        started.blackhole = False
        await dispose_engine()
        started.stop()


async def _timed_get(client: httpx.AsyncClient) -> tuple[int, float]:
    started = time.monotonic()
    response = await asyncio.wait_for(client.get("/readyz"), timeout=RECOVERY_DEADLINE_SECONDS)
    return response.status_code, time.monotonic() - started


async def test_readyz_503_within_bound_on_hung_connection_then_recovers(
    proxy: BlackholeProxy,
) -> None:
    from app.main import create_app
    from app.services.health import READINESS_CACHE_SECONDS, READINESS_TIMEOUT_SECONDS

    bound = READINESS_TIMEOUT_SECONDS + 1.0
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        code, _ = await _timed_get(client)
        assert code == 200, "the warm-up through the proxy must be ready"

        proxy.blackhole = True
        await asyncio.sleep(READINESS_CACHE_SECONDS + 0.2)
        hung = await asyncio.gather(*(_timed_get(client) for _ in range(5)))
        assert [code for code, _ in hung] == [503] * 5
        slowest = max(elapsed for _, elapsed in hung)
        assert slowest < bound, f"/readyz took {slowest:.2f}s on a hung connection"

        # The hung connection drains in the background; until it does, probes join that check
        # (no new pooled connection) and still answer within the bound.
        proxy.blackhole = False
        deadline = time.monotonic() + RECOVERY_DEADLINE_SECONDS
        while True:
            code, elapsed = await _timed_get(client)
            assert elapsed < bound, f"/readyz took {elapsed:.2f}s while the check drained"
            if code == 200:
                break
            assert code == 503
            assert time.monotonic() < deadline, "/readyz never recovered"
            await asyncio.sleep(0.5)
