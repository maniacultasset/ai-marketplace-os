"""Shared helpers for the S12 product suites (CODE-PLAN S12 T3-T12)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]


def seed_dictionary(conn: Conn) -> None:
    """The S12 dictionary rows (`seed_ops.ATTRIBUTE_DEFINITION_ROWS`) on the admin connection,
    left alone when already stored, as the seed itself does."""
    from app.db.seed_ops import ATTRIBUTE_DEFINITION_ROWS

    for row in ATTRIBUTE_DEFINITION_ROWS:
        columns = list(row)
        conn.execute(
            f"INSERT INTO attribute_definitions ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join(['%s'] * len(columns))}) ON CONFLICT (key) DO NOTHING",
            [row[c] for c in columns],
        )
    conn.execute(
        "UPDATE attribute_definitions SET enabled = true WHERE key IN ('color', 'fabric_type')"
    )


@contextmanager
def statements() -> Iterator[list[str]]:
    """Every SQL statement the app's engine sends while the block runs."""
    from app.db.session import get_engine
    from sqlalchemy import event

    seen: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def as_json(value: Any) -> str:
    return json.dumps(value)


_PROVENANCE = json.dumps(
    {
        "title": {"source": "seller", "captured_via": "form", "updated_at": "2026-10-06T00:00:00Z"},
        "category": {
            "source": "seller",
            "captured_via": "form",
            "updated_at": "2026-10-06T00:00:00Z",
        },
    }
)


def insert_products(
    conn: Conn, tenant_id: Any, count: int, *, prefix: str = "S12-ROW", category: str = "kurtas"
) -> list[Any]:
    """`count` products in one statement (so they share one `created_at`, the keyset tiebreak
    case), on the admin connection. Returns their ids."""
    rows = conn.execute(
        "INSERT INTO products (tenant_id, sku, title, category, provenance) "
        "SELECT %s, %s || '-' || n, 'Row ' || n, %s, %s::jsonb FROM generate_series(1, %s) AS n "
        "RETURNING id",
        [tenant_id, prefix, category, _PROVENANCE, count],
    ).fetchall()
    return [r[0] for r in rows]


def soft_delete(conn: Conn, product_id: Any) -> None:
    conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [product_id])
