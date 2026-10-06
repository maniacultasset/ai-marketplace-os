"""S9 T3 (AC 12): the live schema and the mappers agree. Every table in `public` is mapped
except `alembic_version` and the procrastinate queue tables (s8 PLAN.md:107-109), and every
table holding a `tenant_id` column is mapped as `TenantScoped`."""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[object, ...]]


def _mapped() -> dict[str, type]:
    import app.db.models  # noqa: F401
    from app.db.base import Base

    return {m.class_.__table__.name: m.class_ for m in Base.registry.mappers}


def _public_tables(conn: Conn) -> set[str]:
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {str(r[0]) for r in rows}


def test_unmapped_tables_allowlist(db: Conn) -> None:
    unmapped = _public_tables(db) - set(_mapped())
    extra = {t for t in unmapped if t != "alembic_version" and not t.startswith("procrastinate_")}
    assert extra == set()


def test_every_tenant_id_column_table_is_mapped(db: Conn) -> None:
    from app.db.models._tenant import TenantScoped

    rows = db.execute(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t USING (table_schema, table_name) "
        "WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id' "
        "AND t.table_type = 'BASE TABLE'"  # 0004's display views are not tables
    ).fetchall()
    with_tenant = {str(r[0]) for r in rows}
    mapped = _mapped()
    assert with_tenant, "no tenant_id column found: is the database at head?"
    not_scoped = sorted(
        t for t in with_tenant if not issubclass(mapped.get(t, object), TenantScoped)
    )
    assert not_scoped == []
