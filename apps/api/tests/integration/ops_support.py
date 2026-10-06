"""Test support for the 0003 tables: a two-tenant graph of 0001/0002 rows (built on the S6/S7
factories) and the 0003 row factories. `expect_sqlstate` stays in `conftest.py`.

Each factory inserts one row on the caller's connection and transaction and returns its key.
Keyword arguments override or add columns, as in `factories.insert`."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from . import factories as f

Conn = psycopg.Connection[tuple[Any, ...]]

_counter = itertools.count(1)


def insert_row(conn: Conn, table: str, columns: dict[str, Any], returning: str = "id") -> Any:
    """`factories.insert` with the columns as a dict, for tables with a column named `key`."""
    names = list(columns)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(name) for name in names),
        sql.SQL(", ").join(sql.Placeholder() for _ in names),
        sql.Identifier(returning),
    )
    values = [Jsonb(v) if isinstance(v, dict) else v for v in columns.values()]
    row = conn.execute(query, values).fetchone()
    assert row is not None
    return row[0]


def digest(label: str | int) -> bytes:
    """A 32-byte sha256, distinct per label."""
    return hashlib.sha256(str(label).encode()).digest()


@dataclass(frozen=True)
class TenantSeed:
    tenant_id: UUID
    user_id: UUID  # an owner: what `→ m` names
    product_id: UUID
    account_id: UUID  # an active Amazon account
    listing_id: UUID  # an Amazon listing of product_id on account_id
    version_id: UUID  # version 1 of listing_id
    item_id: UUID  # the standalone item of listing_id
    batch_id: UUID


@dataclass(frozen=True)
class Seed:
    a: TenantSeed
    b: TenantSeed


def seed_tenant(conn: Conn) -> TenantSeed:
    tenant_id = f.tenant(conn)
    user_id = f.member(conn, tenant_id)
    product_id = f.product(conn, tenant_id)
    account_id = f.marketplace_account(conn, tenant_id, connected_by_user_id=user_id)
    listing_id = f.listing(conn, tenant_id, product_id, marketplace_account_id=account_id)
    version_id = f.version(conn, tenant_id, listing_id, generated_by_user_id=user_id)
    item_id = f.listing_item(conn, tenant_id, listing_id, product_id)
    batch_id = f.batch(conn, tenant_id, created_by_user_id=user_id)
    return TenantSeed(
        tenant_id, user_id, product_id, account_id, listing_id, version_id, item_id, batch_id
    )


def seed_graph(conn: Conn) -> Seed:
    """Two tenants, A and B, each with the rows of `TenantSeed`."""
    return Seed(a=seed_tenant(conn), b=seed_tenant(conn))


# ---- 0003 row factories ----


def job_run(conn: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """A queued `bulk_import` job on an `import` entity (no listing needed)."""
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "kind": "bulk_import",
        "entity_type": "import",
        "entity_id": uuid4(),
        "payload_json": {"import_id": str(uuid4())},
        "correlation_id": uuid4(),
    }
    return f.insert(conn, "job_runs", **{**defaults, **columns})  # type: ignore[no-any-return]


# ---- platform tables (written as migrator) ----

TEMPLATE_ATTRIBUTES: dict[str, Any] = {
    "required_attributes": ["fabric_type"],
    "optional_attributes": [],
    "specs": {},
}


def category_template(conn: Conn, **columns: Any) -> UUID:
    """Version 1, current, of a `manual` Amazon template for a fresh category."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "marketplace": "amazon",
        "category": f"category_{n}",
        "version": 1,
        "is_current": True,
        "product_type": "SHIRT",
        "source": "manual",
        "attributes": TEMPLATE_ATTRIBUTES,
    }
    return f.insert(conn, "category_templates", **{**defaults, **columns})  # type: ignore[no-any-return]


def error_map(conn: Conn, **columns: Any) -> UUID:
    """An enabled, not auto-fixable Amazon `code` row for a fresh raw value."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "marketplace": "amazon",
        "match_on": "code",
        "raw_value": f"{8000 + n}",
        "version": 1,
        "error_code": "VALUE_NOT_ALLOWED",
        "error_class": "invalid_value",
        "retry_class": "user_action",
        "severity": "error",
        "auto_fixable": False,
        "plain_text": "The {field} value {value} is not accepted by {marketplace}.",
        "enabled": True,
    }
    return f.insert(conn, "marketplace_error_map", **{**defaults, **columns})  # type: ignore[no-any-return]


def attribute_definition(conn: Conn, **columns: Any) -> str:
    """A non-critical `attributes`-backed text key at the style and standalone levels."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "key": f"attr_{n}",
        "section": "attributes",
        "storage": "attributes",
        "owner_levels": ["style", "standalone"],
        "value_type": "text",
        "input_aliases": [],
        "is_critical": False,
        "allowed_sources": ["seller", "ai"],
        "confirm_agent_captures": False,
        "enabled": True,
    }
    return insert_row(conn, "attribute_definitions", {**defaults, **columns}, "key")  # type: ignore[no-any-return]


def attribute_column(conn: Conn, attribute_key: str, **columns: Any) -> str:
    n = next(_counter)
    defaults: dict[str, Any] = {
        "table_name": "products",
        "column_name": f"col_{n}",
        "attribute_key": attribute_key,
    }
    return insert_row(conn, "attribute_columns", {**defaults, **columns}, "column_name")  # type: ignore[no-any-return]


def category_attribute(conn: Conn, attribute_key: str, **columns: Any) -> str:
    n = next(_counter)
    defaults: dict[str, Any] = {
        "category": f"category_{n}",
        "attribute_key": attribute_key,
        "relevance": "common",
        "display_order": n,
        "enabled": True,
    }
    return insert_row(conn, "category_attributes", {**defaults, **columns}, "category")  # type: ignore[no-any-return]


# ---- imports, import_rows, idempotency_keys ----


def import_run(conn: Conn, side: TenantSeed, **columns: Any) -> UUID:
    """A `queued` import of a CSV in `side`'s tenant, with its own `bulk_import` job."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "storage_key": f"{side.tenant_id}/imports/{n}.csv",
        "filename": f"catalog-{n}.csv",
        "status": "queued",
        "imported_rows": 0,
        "failed_rows": 0,
        "created_by": side.user_id,
    }
    if "job_id" not in columns:
        defaults["job_id"] = job_run(conn, side.tenant_id)
    return f.insert(conn, "imports", **{**defaults, **columns})  # type: ignore[no-any-return]


def import_row(conn: Conn, tenant_id: UUID, import_id: UUID, **columns: Any) -> UUID:
    """A `pending` CSV row (row number 2 onward; row 1 is the header)."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "import_id": import_id,
        "row_number": n + 1,
        "raw_row": {"seller_sku": f"SKU-{n}"},
        "status": "pending",
    }
    return f.insert(conn, "import_rows", **{**defaults, **columns})  # type: ignore[no-any-return]


def idempotency_key(conn: Conn, side: TenantSeed, **columns: Any) -> str:
    n = next(_counter)
    defaults: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "key": f"idem-{n}",
        "endpoint": "POST /api/v1/listings/generate",
        "request_hash": digest(n),
        "user_id": side.user_id,
        "expires_at": "2026-10-06T00:00:00Z",
    }
    return insert_row(conn, "idempotency_keys", {**defaults, **columns}, "key")  # type: ignore[no-any-return]


# ---- refresh_tokens, oauth_states ----


def refresh_token(conn: Conn, side: TenantSeed, **columns: Any) -> UUID:
    """A live token of `side`'s owner, in a family of its own."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "user_id": side.user_id,
        "token_hash": digest(f"token-{n}"),
        "expires_at": "2026-10-19T00:00:00Z",
        "family_id": uuid4(),
    }
    return f.insert(conn, "refresh_tokens", **{**defaults, **columns})  # type: ignore[no-any-return]


def oauth_state(conn: Conn, side: TenantSeed, **columns: Any) -> UUID:
    """An Amazon OAuth state of `side`'s owner, valid for ten minutes."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "state_hash": digest(f"state-{n}"),
        "user_id": side.user_id,
        "marketplace": "amazon",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    return f.insert(conn, "oauth_states", **{**defaults, **columns})  # type: ignore[no-any-return]


# ---- listing_submissions, publish_field_results ----


def submission(conn: Conn, side: TenantSeed, **columns: Any) -> UUID:
    """A `pending` attempt-1 `create` of `side`'s listing version, requested by its owner,
    with its own `listing_publish` job unless one is given."""
    n = next(_counter)
    defaults: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "listing_id": side.listing_id,
        "listing_version_id": side.version_id,
        "marketplace": "amazon",
        "marketplace_account_id": side.account_id,
        "attempt": 1,
        "operation": "create",
        "payload_sha256": digest(f"payload-{n}"),
        "payload_storage_key": f"{side.tenant_id}/submissions/{n}.json",
        "facts_digest": digest(f"facts-{n}"),
        "idempotency_key": digest(f"idem-{n}").hex(),
        "requested_by_user_id": side.user_id,
        "actor_type": "user",
    }
    if "job_id" not in columns:
        defaults["job_id"] = job_run(conn, side.tenant_id, created_by=side.user_id)
    return f.insert(conn, "listing_submissions", **{**defaults, **columns})  # type: ignore[no-any-return]


def field_result(conn: Conn, side: TenantSeed, submission_id: UUID, **columns: Any) -> UUID:
    """A `succeeded` result for `content.title` of `side`'s item."""
    defaults: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "submission_id": submission_id,
        "listing_id": side.listing_id,
        "listing_version_id": side.version_id,
        "listing_item_id": side.item_id,
        "field": "content.title",
        "status": "succeeded",
    }
    return f.insert(conn, "publish_field_results", **{**defaults, **columns})  # type: ignore[no-any-return]


def make_publishable(conn: Conn, side: TenantSeed, version_id: UUID | None = None) -> UUID:
    """0004's publish gate: `side`'s listing approved, with `version_id` (default version 1)
    current and approved by a new reviewer (never its generator). The status is placed directly
    (user triggers skipped for that one set-up statement); returns the reviewer."""
    version = side.version_id if version_id is None else version_id
    reviewer = f.member(conn, side.tenant_id, role="reviewer")
    conn.execute(
        "UPDATE listings SET current_version_id = %s WHERE id = %s", (version, side.listing_id)
    )
    conn.execute(
        "UPDATE listing_versions SET approved_by_user_id = %s WHERE id = %s", (reviewer, version)
    )
    conn.execute("SET LOCAL session_replication_role = replica")
    conn.execute(
        "UPDATE listings SET approved_version_id = %s, status = 'approved', last_event = 'approve' "
        "WHERE id = %s",
        (version, side.listing_id),
    )
    conn.execute("SET LOCAL session_replication_role = origin")
    return reviewer
