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

from . import factories as f

Conn = psycopg.Connection[tuple[Any, ...]]

_counter = itertools.count(1)


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
