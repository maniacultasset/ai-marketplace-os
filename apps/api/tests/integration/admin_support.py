"""Shared fixtures for the S14b admin suites (T17-T22): the app with the admin mounted, as `app_rw`
on the emptied test database, in `local` or `staging`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.support.deployed_env import DEPLOYED_S15_ENV

from . import factories as f
from .auth_support import World
from .jobs_support import insert_job

Conn = psycopg.Connection[tuple[Any, ...]]

ADMIN = "/internal/admin"
DSN = "https://public@o0.ingest.example.invalid/0"
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"
PRIVATE_ADMIN = "Zq9-admin-session-not-committed-32+"

AdminClient = Callable[..., Any]


@pytest.fixture
def admin_client(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AdminClient:
    """`async with admin_client(app_env="local") as client:` an httpx client on a fresh
    `create_app()` with `ADMIN_ENABLED=true`."""

    @asynccontextmanager
    async def open_client(
        app_env: str = "local", *, enabled: bool = True
    ) -> AsyncIterator[httpx.AsyncClient]:
        import app.main
        from app.core.config import get_settings

        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv("ADMIN_ENABLED", "true" if enabled else "false")
        if app_env != "local":
            monkeypatch.setenv("SENTRY_DSN", DSN)
            monkeypatch.setenv("JWT_SECRET", PRIVATE_JWT)
            monkeypatch.setenv("ADMIN_SESSION_SECRET", PRIVATE_ADMIN)
            for key, value in DEPLOYED_S15_ENV.items():
                monkeypatch.setenv(key, value)
            monkeypatch.setattr(app.main, "init_sentry", lambda _: False)
        get_settings.cache_clear()
        transport = httpx.ASGITransport(app=app.main.create_app(), client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
        get_settings.cache_clear()

    return open_client


class Links(HTMLParser):
    """Every `src`/`href` attribute and CSS `url(...)` in a page."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self._style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href"} and value:
                self.refs.append(value)
            if name == "style" and value:
                self.refs += _css_urls(value)
        self._style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._style = False

    def handle_data(self, data: str) -> None:
        if self._style:
            self.refs += _css_urls(data)


def _css_urls(text: str) -> list[str]:
    import re

    return [m.strip("'\" ") for m in re.findall(r"url\(([^)]*)\)", text)]


def external_refs(html: str, own_host: str = "testserver") -> list[str]:
    """References that leave the admin: another scheme or host, or a path outside the mount.
    sqladmin builds absolute same-origin URLs (`url_for`), which stay allowed."""
    from urllib.parse import urlsplit

    parser = Links()
    parser.feed(html)
    bad = []
    for ref in parser.refs:
        if ref.startswith(("#", "?")) or ref == "javascript:void(0)":
            continue
        parts = urlsplit(ref)
        if parts.scheme and (parts.scheme not in {"http", "https"} or parts.netloc != own_host):
            bad.append(ref)
        elif not parts.scheme and parts.netloc:
            bad.append(ref)  # a protocol-relative //host
        elif parts.path.startswith("/") and not parts.path.startswith(ADMIN):
            bad.append(ref)
    return bad


# ---- T21-T22: the ops world (two tenants, fixed timestamps) ----

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
REPORT = {"status": "failed", "error_code": "JOB_TIMEOUT", "errors": []}


@dataclass
class OpsWorld:
    world: World
    conn: Conn
    queue: str


def _job(conn: Conn, tenant: uuid.UUID, status: str, **columns: Any) -> uuid.UUID:
    values: dict[str, Any] = {"status": status}
    if status in ("succeeded", "failed", "dead_letter", "cancelled", "partial"):
        values.update(attempt=1, started_at=NOW - timedelta(hours=2))
        values.setdefault("finished_at", NOW - timedelta(hours=1))
    if status == "running":
        values.update(attempt=1, started_at=NOW, lease_expires_at=NOW + timedelta(minutes=5))
    if status in ("failed", "dead_letter"):
        values.update(error_report_json=Jsonb(REPORT), error_code="JOB_TIMEOUT")
    values.update(columns)
    return insert_job(conn, tenant, **values)


def _listing(
    conn: Conn,
    tenant: uuid.UUID,
    status: str,
    changed: datetime,
    first_published_at: datetime | None = None,
) -> None:
    """A listing moved straight to `status` (the status guards are skipped for the fixture row;
    the CHECKs still hold: a published row names its version and a marketplace state)."""
    listing_id = f.listing(conn, tenant)
    columns: dict[str, Any] = {"status": status, "status_changed_at": changed}
    if status == "published":
        columns.update(
            published_version_id=f.version(conn, tenant, listing_id),
            first_published_at=first_published_at,
            marketplace_state="processing",
        )
    sets = ", ".join(f"{name} = %s" for name in columns)
    conn.execute("SET session_replication_role = replica")
    try:
        conn.execute(
            f"UPDATE listings SET {sets} WHERE id = %s",  # noqa: S608 - fixed column names
            [*columns.values(), listing_id],
        )
    finally:
        conn.execute("RESET session_replication_role")


@pytest.fixture
def ops_world(products_world: World, auth_db: Conn) -> OpsWorld:
    """Jobs, agent spend and listings for `acme` and `bharat` around `NOW`."""
    from app.workers.policy import POLICIES
    from mkos_schemas.enums import JobKind

    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    _job(auth_db, acme, "queued", queued_at=NOW - timedelta(seconds=600))
    _job(auth_db, bharat, "queued", queued_at=NOW - timedelta(seconds=60))
    _job(auth_db, acme, "running", queued_at=NOW - timedelta(seconds=30))
    _job(auth_db, acme, "succeeded")
    for n in range(3):
        _job(auth_db, bharat, "dead_letter", finished_at=NOW - timedelta(minutes=n))
    # spend: the IST/UTC boundary and the UTC midnight
    for tenant, at, cost in (
        (acme, "2026-10-04T18:29:59Z", "0.100000"),  # 23:59:59 IST on 4 Oct
        (acme, "2026-10-04T18:30:00Z", "0.200000"),  # 00:00:00 IST on 5 Oct, still 4 Oct UTC
        (bharat, "2026-10-04T23:59:59Z", "0.050000"),
        (bharat, "2026-10-05T00:00:00Z", "0.400000"),
        (acme, "2026-09-01T00:00:00Z", "9.000000"),  # outside every window
    ):
        f.agent_run(auth_db, tenant, cost_usd=Decimal(cost), created_at=at, started_at=at)
    _listing(
        auth_db,
        acme,
        "published",
        NOW - timedelta(hours=1),
        first_published_at=NOW - timedelta(days=2),
    )
    _listing(
        auth_db,
        bharat,
        "published",
        NOW - timedelta(hours=30),
        first_published_at=NOW - timedelta(days=40),
    )
    _listing(auth_db, acme, "created", NOW - timedelta(hours=2))
    _listing(auth_db, bharat, "created", NOW - timedelta(hours=3))
    _listing(auth_db, bharat, "created", NOW - timedelta(hours=25))  # outside the 24 h window
    return OpsWorld(products_world, auth_db, POLICIES[JobKind.BULK_IMPORT].queue)
