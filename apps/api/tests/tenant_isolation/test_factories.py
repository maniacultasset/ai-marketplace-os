"""S9 T16-T18: the isolation world holds one row per TenantScoped table for each tenant."""

from __future__ import annotations

import psycopg
import pytest
from app.db.base import Base
from app.db.models._tenant import TenantScoped
from psycopg import sql

from .factories import build_world
from .factories import table_name as _table_name

pytestmark = pytest.mark.usefixtures("clean_tenants")

Conn = psycopg.Connection[tuple[object, ...]]


def test_world_builds_for_two_tenants(admin_conn: Conn) -> None:
    world = build_world(admin_conn, 2)
    tables = {
        _table_name(m.class_) for m in Base.registry.mappers if issubclass(m.class_, TenantScoped)
    }
    assert set(world.rows) == tables
    for table in sorted(tables):
        column = "id" if table == "tenants" else "tenant_id"
        query = sql.SQL("SELECT {c}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY 1").format(
            c=sql.Identifier(column), t=sql.Identifier(table)
        )
        counts: dict[object, object] = {r[0]: r[1] for r in admin_conn.execute(query).fetchall()}
        for tenant in world.tenants:
            assert int(str(counts.get(tenant, 0))) >= 1, (table, tenant)
