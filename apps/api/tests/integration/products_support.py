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


# ---- S12 T8: bearers for the route suites (minted, never logged in: the login limit stays out) ----

PRODUCTS = "/api/v1/products"
PRODUCT_FIELDS = frozenset(
    {
        "id",
        "sku",
        "title",
        "status",
        "category",
        "attributes",
        "images",
        "parent_product_id",
        "created_at",
        "updated_at",
    }
)
OTHER_SECRET = "s12_not_the_ci_secret_at_least_32_characters"


def slug_of(email: str) -> str:
    return email.split("@", 1)[1].split(".", 1)[0]


def bearer_for(
    world: Any, email: str, *, tenant: str | None = None, role: str | None = None
) -> dict[str, str]:
    """A signed access token for a world user; `tenant` swaps the `tid`, `role` the role claim."""
    from .auth_support import bearer, tokens_for

    return bearer(tokens_for(world, email, tenant or slug_of(email), role))


def bad_bearer(world: Any, variant: str, email: str = "operator@acme.test") -> dict[str, str]:
    """`expired`, `bad_signature` (signed with another secret) or `wrong_typ` (a refresh token)."""
    import time

    import jwt
    from app.core.security import issue_claims

    from .auth_support import bearer, tokens_for

    if variant == "wrong_typ":
        return bearer(tokens_for(world, email, slug_of(email), typ="refresh"))
    claims = issue_claims(
        sub=world.user(email),
        tid=world.tenant(slug_of(email)),
        role=email.split("@", 1)[0],  # type: ignore[arg-type]
        email=email,
        typ="access",
    ).model_dump(mode="json")
    if variant == "expired":
        now = int(time.time())
        claims.update(iat=now - 3600, exp=now - 1)
        from tests.conftest import CI_JWT_SECRET

        return bearer(jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256"))
    if variant == "bad_signature":
        return bearer(jwt.encode(claims, OTHER_SECRET, algorithm="HS256"))
    raise ValueError(variant)


def counts(conn: Conn) -> tuple[int, int]:
    """(products, product.created audit rows) on the admin connection."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM products), "
        "(SELECT count(*) FROM audit_log WHERE action = 'product.created')"
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def without_correlation(body: dict[str, Any]) -> dict[str, Any]:
    """An error body with its request-specific ids blanked, for byte-equality checks."""
    error = dict(body["error"])
    error["correlation_id"] = "-"
    report = dict(error["error_report"])
    report["request_id"] = "-"
    error["error_report"] = report
    return {"error": error}


def site_body(sku: str = "S12-KURTA-1", **overrides: Any) -> dict[str, Any]:
    """The spec's create body (L11317-11322), with a category (DEVIATIONS T0, D3)."""
    body: dict[str, Any] = {
        "sku": sku,
        "title": "Organic Cotton Kurta",
        "category": "kurtas",
        "attributes": {"color": "navy blue", "material": "organic cotton"},
    }
    body.update(overrides)
    return body
