"""AC6b against a real Postgres: 200 on `SELECT 1`, else 503 with no detail."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from sqlalchemy import event

if TYPE_CHECKING:
    from app.core.config import Settings

UNAVAILABLE = {"status": "unavailable", "checks": {"postgres": "unavailable"}}

# pytest-socket blocks socket.getaddrinfo, which psycopg calls to resolve the host before libpq
# connects. Database tests may resolve and reach the loopback test database only.
pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


def _client() -> httpx.AsyncClient:
    from app.main import create_app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t")


def _replace_url(url: str, *, password: str | None = None, port: int | None = None) -> str:
    parts = urlsplit(url)
    user = parts.username or "postgres"
    secret = password if password is not None else (parts.password or "")
    host = f"{parts.hostname}:{port if port is not None else parts.port}"
    return urlunsplit(parts._replace(netloc=f"{user}:{secret}@{host}"))


async def _reset_settings(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()


async def test_readyz_200_real_postgres(db_settings: Settings) -> None:
    async with _client() as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"postgres": "ok"}}


async def test_readyz_503_bad_password_no_leak(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert db_settings.database_url is not None
    await _reset_settings(monkeypatch, _replace_url(db_settings.database_url, password="Zq9wrong"))
    with caplog.at_level(logging.DEBUG):
        async with _client() as client:
            response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert "Zq9" not in response.text
    assert "Zq9" not in caplog.text


async def test_readyz_503_unreachable_port_within_timeout(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.health import READINESS_TIMEOUT_SECONDS

    assert db_settings.database_url is not None
    await _reset_settings(monkeypatch, _replace_url(db_settings.database_url, port=1))
    started = time.monotonic()
    async with _client() as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert time.monotonic() - started < READINESS_TIMEOUT_SECONDS + 1.0


async def test_readyz_20_concurrent_calls_all_200(db_settings: Settings) -> None:
    from app.db.session import get_engine

    selects: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.strip().upper() == "SELECT 1":
            selects.append(statement)

    event.listen(get_engine().sync_engine, "before_cursor_execute", count)
    async with _client() as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(20)))
    assert [r.status_code for r in responses] == [200] * 20
    assert len(selects) == 1


async def test_health_healthz_200_db_unreachable(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness never depends on the database: port 1 refuses every connection."""
    assert db_settings.database_url is not None
    await _reset_settings(monkeypatch, _replace_url(db_settings.database_url, port=1))
    async with _client() as client:
        healthz = await client.get("/healthz")
        readyz = await client.get("/readyz")
    assert healthz.status_code == 200
    assert healthz.json() == {"status": "ok"}
    assert readyz.status_code == 503
    assert readyz.json() == UNAVAILABLE
