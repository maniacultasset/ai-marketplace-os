"""Row factories for the 0001 schema tests. Each inserts one row on the given connection (the
caller's transaction) and returns its id. Keyword arguments override or add columns; a dict
value is sent as jsonb, a list as a PostgreSQL array (wrap it in `Jsonb` for a jsonb array)."""

from __future__ import annotations

import itertools
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

Conn = psycopg.Connection[tuple[Any, ...]]

_counter = itertools.count(1)


def _next() -> int:
    return next(_counter)


def fact(source: Any = "seller", captured_via: Any = "form", **fields: Any) -> dict[str, Any]:
    """One fact envelope (DATABASE.md §5.6). `value` defaults to "x"; pass `value=None` to omit
    it. A non-seller source gets an `image:` evidence ref unless one is given."""
    envelope: dict[str, Any] = {
        "value": "x",
        "source": source,
        "captured_via": captured_via,
        "updated_at": "2026-10-05T00:00:00Z",
    }
    if source != "seller":
        envelope["evidence_ref"] = "image:abc"
    if source in ("ai", "derived"):
        envelope["confidence"] = 0.9
    if captured_via == "agent":
        envelope["agent_run_id"] = "00000000-0000-0000-0000-000000000001"
    envelope.update(fields)
    return {key: value for key, value in envelope.items() if value is not None}


def _adapt(value: Any) -> Any:
    return Jsonb(value) if isinstance(value, dict) else value


def insert(conn: Conn, table: str, key: str = "id", **columns: Any) -> Any:
    """INSERT one row and return its `key` column."""
    names = list(columns)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(name) for name in names),
        sql.SQL(", ").join(sql.Placeholder() for _ in names),
        sql.Identifier(key),
    )
    row = conn.execute(query, [_adapt(columns[name]) for name in names]).fetchone()
    assert row is not None
    return row[0]


def tenant(conn: Conn, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {"name": f"Tenant {n}", "slug": f"tenant-{n}", "status": "active"}
    return insert(conn, "tenants", **{**defaults, **columns})  # type: ignore[no-any-return]


def user(conn: Conn, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {"email": f"user{n}@x.test", "hashed_password": "argon2id-hash"}
    return insert(conn, "users", **{**defaults, **columns})  # type: ignore[no-any-return]


def member(conn: Conn, tenant_id: UUID, role: str = "owner", **columns: Any) -> UUID:
    """A new user with a membership in `tenant_id`; returns the user id (what `→ m` names)."""
    user_id = user(conn)
    insert(conn, "tenant_memberships", tenant_id=tenant_id, user_id=user_id, role=role, **columns)
    return user_id


def party(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "roles": ["manufacturer"],
        "legal_name": f"Party {n} Pvt Ltd",
        "source": "seller",
    }
    return insert(conn, "business_parties", **{**defaults, **columns})  # type: ignore[no-any-return]


def brand_profile(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    return insert(  # type: ignore[no-any-return]
        conn, "brand_profiles", key="tenant_id", tenant_id=tenant_id, **columns
    )


# Typed fact column → its `provenance` key (DATABASE.md §5.6 `ck_products_typed_facts_sourced`).
TYPED_FACT_KEYS: dict[str, str] = {
    "mrp": "mrp",
    "selling_price": "selling_price",
    "tax_rate": "tax_rate",
    "hsn_code": "hsn_code",
    "country_of_origin": "country_of_origin",
    "generic_name": "generic_name",
    "net_quantity": "net_quantity",
    "identifier_status": "identifier",
    "package_length_cm": "package_dimensions",
    "package_weight_g": "package_weight",
    "product_weight_g": "product_weight",
    "manufacturer_party_id": "manufacturer",
    "marketer_party_id": "marketer",
    "packer_party_id": "packer",
    "importer_party_id": "importer",
    "consumer_care_party_id": "consumer_care",
    "brand_name": "brand_name",
    "sub_category": "sub_category",
    "product_type": "product_type",
    "gender": "gender",
}


def product(
    conn: Conn,
    tenant_id: UUID,
    kind: str = "standalone",
    *,
    parent: UUID | None = None,
    **columns: Any,
) -> UUID:
    """A `style` (theme `size_color`), a `child` of `parent` or a `standalone` product. Unless
    `provenance` is given, it holds a seller fact for title, category and every typed fact
    column passed, which is what `ck_products_typed_facts_sourced` needs."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "sku": f"SKU-{n}",
        "title": f"Product {n}",
        "category": "t_shirts",
    }
    if "provenance" not in columns:
        keys = ["title", "category"]
        keys += [key for col, key in TYPED_FACT_KEYS.items() if columns.get(col) is not None]
        defaults["provenance"] = {key: fact() for key in keys}
    if kind == "style":
        defaults["variation_theme"] = "size_color"
    elif kind == "child":
        assert parent is not None, "a child needs its style"
        defaults["parent_product_id"] = parent
        defaults["variant_attributes"] = {"size": f"s{n}"}
    else:
        assert kind == "standalone", kind
    return insert(conn, "products", **{**defaults, **columns})  # type: ignore[no-any-return]


def colourway(conn: Conn, tenant_id: UUID, style_id: UUID, **columns: Any) -> UUID:
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "style_product_id": style_id,
        "attributes": {"color": fact(value=f"Colour {n}")},
        "position": 1,
    }
    return insert(conn, "product_colourways", **{**defaults, **columns})  # type: ignore[no-any-return]


def cell(conn: Conn, tenant_id: UUID, style_id: UUID, **columns: Any) -> UUID:
    """An `unavailable/not_produced` cell for size `s<n>` unless the columns say otherwise."""
    n = _next()
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "style_product_id": style_id,
        "size_key": f"s{n}",
        "availability": "unavailable",
        "unavailable_reason": "not_produced",
        "source": "seller",
    }
    return insert(conn, "variant_matrix_cells", **{**defaults, **columns})  # type: ignore[no-any-return]
