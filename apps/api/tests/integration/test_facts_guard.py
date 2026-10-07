"""S13 B5a (Q13, AC 24; DATABASE.md:1180-1186, :1797): revision 0006 `facts_guard`, as app_rw.

Only keys whose envelope changed are checked against the synced dictionary: unknown, disabled or
wrongly stored → PRV02; the row's level outside `owner_levels` → PRV03; a source outside
`allowed_sources`, or `ai` on a critical or `critical_override` key → PRV01."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from .conftest import LOCAL_HOSTS, Conn, ScratchDb, libpq_url

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
STAMP = "2026-10-07T00:00:00Z"


def seller(**extra: Any) -> dict[str, Any]:
    return {
        "value": "x",
        "source": "seller",
        "captured_via": "import",
        "updated_at": STAMP,
        **extra,
    }


def ai(**extra: Any) -> dict[str, Any]:
    return {
        "value": "x",
        "source": "ai",
        "captured_via": "agent",
        "confidence": 0.9,
        "evidence_ref": "image:abc",
        "agent_run_id": str(uuid.uuid4()),
        "updated_at": STAMP,
        **extra,
    }


PROVENANCE = {"title": {k: v for k, v in seller().items() if k != "value"}, "category": seller()}


@pytest.fixture
def tenant(clean_tenant_db: None, superuser_conn: Conn) -> uuid.UUID:
    from .auth_support import add_tenant

    return add_tenant(superuser_conn, f"facts-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def app_rw(tenant: uuid.UUID) -> Iterator[Conn]:
    from tests.support.dsn import as_app_rw

    url = libpq_url(as_app_rw(os.environ["DATABASE_URL"]))
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", [str(tenant)])
        yield conn


def insert(conn: Conn, tenant: uuid.UUID, **columns: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "tenant_id": tenant,
        "sku": f"FG-{uuid.uuid4().hex[:10]}",
        "title": "Kurta",
        "category": "kurtas",
        "provenance": PROVENANCE,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    row = conn.execute(
        f"INSERT INTO products ({names}) VALUES ({marks}) RETURNING id",  # noqa: S608
        [Jsonb(v) if isinstance(v, dict) else v for v in values.values()],
    ).fetchone()
    assert row is not None and isinstance(row[0], uuid.UUID)
    return row[0]


def refused(code: str, conn: Conn, tenant: uuid.UUID, **columns: Any) -> None:
    with pytest.raises(psycopg.Error) as caught:
        insert(conn, tenant, **columns)
    assert caught.value.sqlstate == code
    assert "x" not in str(caught.value).split("\n")[0].split()


@pytest.fixture
def override_category(superuser_conn: Conn) -> Iterator[str]:
    """A fresh category whose `fit` key carries `critical_override` (disabled afterwards: the
    dictionary guard refuses DELETE)."""
    category = f"cat_{uuid.uuid4().hex[:10]}"
    superuser_conn.execute(
        "INSERT INTO category_attributes (category, attribute_key, relevance, critical_override, "
        "display_order, enabled) VALUES (%s, 'fit', 'core', true, 1, true)",
        [category],
    )
    yield category
    superuser_conn.execute(
        "UPDATE category_attributes SET enabled = false WHERE category = %s", [category]
    )


def test_facts_guard_ai_on_critical_prv01(app_rw: Conn, tenant: uuid.UUID) -> None:
    refused("PRV01", app_rw, tenant, attributes={"fabric_type": ai()})
    refused("PRV01", app_rw, tenant, provenance={**PROVENANCE, "mrp": ai()}, mrp=100)
    insert(app_rw, tenant, attributes={"fit": ai()})  # not critical: ai allowed


def test_facts_guard_ai_on_critical_override_prv01(
    app_rw: Conn, tenant: uuid.UUID, override_category: str
) -> None:
    refused("PRV01", app_rw, tenant, category=override_category, attributes={"fit": ai()})
    insert(app_rw, tenant, category=override_category, attributes={"fit": seller()})
    insert(app_rw, tenant, attributes={"fit": ai()})  # another category: no override


def test_facts_guard_old_ai_value_other_key_writable(
    app_rw: Conn, tenant: uuid.UUID, superuser_conn: Conn, override_category: str
) -> None:
    """DATABASE.md:1797: a key that turned critical after an ai value was stored does not block
    a write to another key of the same product."""
    superuser_conn.execute(
        "UPDATE category_attributes SET enabled = false WHERE category = %s", [override_category]
    )
    product = insert(app_rw, tenant, category=override_category, attributes={"fit": ai()})
    superuser_conn.execute(
        "UPDATE category_attributes SET enabled = true WHERE category = %s", [override_category]
    )
    app_rw.execute(
        "UPDATE products SET attributes = attributes || %s WHERE id = %s",
        [Jsonb({"pattern": seller()}), product],
    )
    with pytest.raises(psycopg.Error) as caught:
        app_rw.execute(
            "UPDATE products SET attributes = attributes || %s WHERE id = %s",
            [Jsonb({"fit": ai(value="y")}), product],
        )
    assert caught.value.sqlstate == "PRV01"


def test_facts_guard_unknown_key_prv02(app_rw: Conn, tenant: uuid.UUID) -> None:
    refused("PRV02", app_rw, tenant, attributes={"no_such_key": seller()})
    refused("PRV02", app_rw, tenant, attributes={"mrp": seller()})  # a column key
    refused("PRV02", app_rw, tenant, provenance={**PROVENANCE, "color": seller()})


def test_facts_guard_disabled_key_prv02(
    app_rw: Conn, tenant: uuid.UUID, superuser_conn: Conn
) -> None:
    superuser_conn.execute("UPDATE attribute_definitions SET enabled = false WHERE key = 'hem'")
    try:
        refused("PRV02", app_rw, tenant, attributes={"hem": seller()})
    finally:
        superuser_conn.execute("UPDATE attribute_definitions SET enabled = true WHERE key = 'hem'")


def test_facts_guard_level_invalid_prv03(app_rw: Conn, tenant: uuid.UUID) -> None:
    # `brand_names` is a brand-level key; `size` sits on a variant or a standalone row only
    refused("PRV03", app_rw, tenant, provenance={**PROVENANCE, "brand_names": seller()})
    refused(
        "PRV03",
        app_rw,
        tenant,
        variation_theme="size",
        size_run=["m"],
        attributes={"size": seller()},
    )
    with pytest.raises(psycopg.Error) as caught:
        app_rw.execute(
            "INSERT INTO brand_profiles (tenant_id, provenance) VALUES (%s, %s)",
            [tenant, Jsonb({"mrp": seller()})],
        )
    assert caught.value.sqlstate == "PRV03"


def test_facts_guard_level_change_rechecks_every_key(app_rw: Conn, tenant: uuid.UUID) -> None:
    # S13b review #1: a standalone row keeps `size`; turning it into a style (variation_theme set)
    # leaves the key unchanged but its level is now wrong, so the guard re-checks every key.
    product = insert(app_rw, tenant, attributes={"size": seller()})
    with pytest.raises(psycopg.Error) as caught:
        app_rw.execute(
            "UPDATE products SET variation_theme = 'size', size_run = %s WHERE id = %s",
            [["m"], product],
        )
    assert caught.value.sqlstate == "PRV03"
    # dropping the variant-only key in the same write makes the change legal
    app_rw.execute(
        "UPDATE products SET variation_theme = 'size', size_run = %s, attributes = '{}' "
        "WHERE id = %s",
        [["m"], product],
    )


def test_facts_guard_unchanged_key_not_rechecked(
    app_rw: Conn, tenant: uuid.UUID, superuser_conn: Conn
) -> None:
    with superuser_conn.transaction():
        superuser_conn.execute("SET LOCAL session_replication_role = replica")
        product = insert(superuser_conn, tenant, attributes={"legacy_key": seller()})
    app_rw.execute(
        "UPDATE products SET attributes = attributes || %s, title = 'Renamed' WHERE id = %s",
        [Jsonb({"fit": seller()}), product],
    )
    row = superuser_conn.execute(
        "SELECT attributes ? 'legacy_key', title FROM products WHERE id = %s", [product]
    ).fetchone()
    assert row == (True, "Renamed")


def test_facts_guard_import_good_rows_pass(app_rw: Conn, tenant: uuid.UUID) -> None:
    """The facts an import row writes (seller, `captured_via` import) on a standalone product,
    a style, a colourway and a variant pass the guard."""
    stamp = seller()
    column_facts = {k: {kk: vv for kk, vv in stamp.items() if kk != "value"} for k in (
        "title", "category", "brand_name", "sub_category", "product_type", "gender", "mrp",
        "selling_price", "tax_rate", "hsn_code", "country_of_origin", "package_dimensions",
        "package_weight", "manufacturer", "consumer_care",
    )}  # fmt: skip
    standalone = {**column_facts, "identifier": column_facts["title"]}
    attributes = {k: seller() for k in ("fit", "pattern", "fabric_type", "wash_care", "color")}
    insert(app_rw, tenant, provenance=standalone, attributes=attributes)
    style_facts = {k: v for k, v in column_facts.items() if k != "identifier"}
    style = insert(
        app_rw,
        tenant,
        provenance=style_facts,
        variation_theme="size_color",
        size_run=["m"],
        attributes={"fit": seller(), "size_system": seller()},
    )
    row = app_rw.execute(
        "INSERT INTO product_colourways (tenant_id, style_product_id, attributes, position) "
        "VALUES (%s, %s, %s, 1) RETURNING id",
        [tenant, style, Jsonb({"color": seller(value="Navy")})],
    ).fetchone()
    assert row is not None
    child_provenance = {k: column_facts[k] for k in ("title", "category", "mrp")}
    with app_rw.transaction():
        child = insert(
            app_rw,
            tenant,
            parent_product_id=style,
            variant_attributes={"size": "m", "color": "navy"},
            provenance=child_provenance,
            mrp=999,
            attributes={"size": seller(value="M", normalized="m"), "fit": seller()},
        )
        app_rw.execute(
            "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, colourway_id, "
            "size_key, availability, variant_product_id, source) "
            "VALUES (%s, %s, %s, 'm', 'available', %s, 'seller')",
            [tenant, style, row[0], child],
        )


def test_facts_guard_migration_round_trip(
    alembic_env: Any, scratch_db: ScratchDb, tmp_path: Path
) -> None:
    cfg: Config = alembic_env(None)
    command.upgrade(cfg, "head")

    def triggers() -> list[tuple[object, ...]]:
        with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
            return conn.execute(
                "SELECT tgrelid::regclass::text FROM pg_trigger "
                "WHERE tgname = 'facts_guard' ORDER BY 1"
            ).fetchall()

    def functions() -> list[tuple[object, ...]]:
        with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
            return conn.execute(
                "SELECT proname FROM pg_proc WHERE proname LIKE 'facts_guard%' ORDER BY 1"
            ).fetchall()

    at_head = triggers()
    assert at_head == [("brand_profiles",), ("product_colourways",), ("products",)]
    assert functions() == [("facts_guard",), ("facts_guard_check",)]
    command.downgrade(cfg, "0005_rls")
    assert triggers() == [] and functions() == []
    command.upgrade(cfg, "head")
    assert triggers() == at_head
    assert json.dumps(functions()) == json.dumps([["facts_guard"], ["facts_guard_check"]])
