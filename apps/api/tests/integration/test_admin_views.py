"""S14b T18 (AC 11): the 12 read-only views select only columns `app_platform_ro` may read, never a
secret column, and every session is `platform_scope()`."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from .conftest import LOCAL_HOSTS
from .seam_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PG: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
TABLES = (
    "tenants",
    "users",
    "products",
    "listings",
    "listing_versions",
    "validation_runs",
    "agent_runs",
    "job_runs",
    "images",
    "rules",
    "marketplace_accounts",
    "audit_log",
)


def _view(table: str) -> Any:
    from app.admin.views import ADMIN_VIEWS

    (view,) = [v for v in ADMIN_VIEWS if v.table == table]
    return view()


def _request(pk: str | None = None) -> Any:
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
            "path_params": {"pk": pk or str(uuid.uuid4())},
        }
    )


def _statements(table: str) -> list[Any]:

    view = _view(table)
    listed = view.list_query(_request())
    return [listed, view.details_query(_request()), view.count_query(_request())]


async def test_admin_list_count_ignores_the_subquery(
    auth_db: Conn, db_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sqladmin's list page passes a count over a subquery of the list query, which drops
    `load_only`; the views count with `count_query` instead (never the secret columns)."""
    from sqlalchemy import func, select

    view = _view("users")
    seen: list[str] = []

    async def record(stmt: Any) -> Any:
        seen.append(str(stmt.compile(dialect=PG)))
        return [0]

    monkeypatch.setattr(view, "_run_query", record)
    subquery = select(func.count()).select_from(view.list_query(_request()).subquery())
    assert "hashed_password" in str(subquery.compile(dialect=PG))
    assert await view.count(_request(), subquery) == 0
    assert seen and "hashed_password" not in seen[0]


def _columns(stmt: Any, table: str) -> set[str]:
    sql = str(stmt.compile(dialect=PG))
    names = set(re.findall(rf"\b{table}\.(\w+)", sql))
    assert names, sql
    return names


def _granted(conn: Conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.column_privileges WHERE table_schema = "
        "'public' AND table_name = %s AND grantee = 'app_platform_ro' AND privilege_type = "
        "'SELECT'",
        [table],
    ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.parametrize("table", TABLES)
async def test_admin_columns_within_platform_ro_grants(
    auth_db: Conn, db_settings: Any, table: str
) -> None:
    from app.admin.session import PlatformSessionMaker

    granted = _granted(auth_db, table)
    assert granted
    for stmt in _statements(table):
        assert _columns(stmt, table) <= granted, table
    # and the role really may run them (a missing column grant is SQLSTATE 42501)
    async with PlatformSessionMaker()() as session:
        for stmt in _statements(table):
            await session.execute(stmt)


@pytest.mark.parametrize("table", TABLES)
def test_admin_never_selects_secret_columns(table: str) -> None:
    from app.admin.views import ADMIN_COLUMNS, NEVER_LISTED

    assert not set(ADMIN_COLUMNS[table]) & NEVER_LISTED
    for stmt in _statements(table):
        assert not _columns(stmt, table) & NEVER_LISTED, table


async def test_admin_session_is_platform_scope(auth_db: Conn, db_settings: Any) -> None:
    import sqlalchemy.exc
    from app.admin.session import PlatformSessionMaker
    from sqlalchemy import text

    maker = PlatformSessionMaker()
    async with maker(expire_on_commit=False) as session:
        assert (await session.execute(text("SELECT current_user"))).scalar() == "app_platform_ro"
        with pytest.raises(sqlalchemy.exc.DBAPIError) as caught:
            await session.execute(
                text("INSERT INTO tenants (name, slug, status) VALUES ('x', 'x_admin', 'active')")
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "25006"


@pytest.mark.parametrize("table", TABLES)
def test_admin_views_flags_all_false(table: str) -> None:
    view = _view(table)
    assert (view.can_create, view.can_edit, view.can_delete, view.can_export) == (
        False,
        False,
        False,
        False,
    )


def test_admin_twelve_views_registered() -> None:
    from app.admin import TITLE
    from app.admin.session import PlatformSessionMaker
    from app.admin.views import ADMIN_VIEWS, register_views
    from fastapi import FastAPI
    from sqladmin import Admin

    assert sorted(v.table for v in ADMIN_VIEWS) == sorted(TABLES)
    admin = Admin(FastAPI(), session_maker=PlatformSessionMaker(), title=TITLE)  # type: ignore[arg-type]
    register_views(admin)
    from sqladmin import ModelView

    assert len([v for v in admin.views if isinstance(v, ModelView)]) == 12
    assert len(admin.views) == 13  # plus the ops pages (T22)
