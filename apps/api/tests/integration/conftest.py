"""Integration fixtures: a real Postgres, and never the dev database `marketplace_os`."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest

if TYPE_CHECKING:
    from app.core.config import Settings

ALLOWED_DATABASES = frozenset({"mos_test", "mos_iso"})


def libpq_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
async def db_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Settings]:
    """Settings for the test database. Fails (never skips) unless DATABASE_URL points at
    mos_test or mos_iso; disposes the engine after each test (one event loop per test)."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is unset: run the integration tests through `make api-tests`")
    with psycopg.connect(libpq_url(url), connect_timeout=5) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    database = row[0] if row else None
    if database not in ALLOWED_DATABASES:
        pytest.fail(f"refusing to run integration tests against database {database!r}")
    monkeypatch.chdir(tmp_path)  # a developer's .env must not leak in
    get_settings.cache_clear()
    await dispose_engine()
    yield get_settings()
    await dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
def superuser_conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """An autocommit connection to the test database (mos_test or mos_iso only)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is unset: run the integration tests through `make api-tests`")
    with psycopg.connect(libpq_url(url), autocommit=True, connect_timeout=5) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
        if not row or row[0] not in ALLOWED_DATABASES:
            pytest.fail(f"refusing to run integration tests against database {row!r}")
        yield conn
