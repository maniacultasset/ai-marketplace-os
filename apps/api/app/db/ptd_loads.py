"""The migrator's writes of Amazon Product Type Definitions (DW2 T8; T53; DATABASE.md §5.2): the
SQL of `app.cli templates refresh` and `templates promote-definition` lives here, inside `app/db`
(repo rule R4). Each runs as `migrator`, under the migrations' advisory lock, in one transaction:
`product_type_definitions` is SELECT-only for the app roles, and its guard lets only
`is_current` change.

- `store_definition`: the same key and digest already stored -> `unchanged`, nothing written (an
  idempotent refresh). Otherwise one INSERT, current only when no row of the key is current yet,
  audited `product_type_definition.stored` (staff, NULL tenant, the allow-listed keys only), and
  `product_type_definition.promoted` when it went in current.
- `promote_definition`: the two ordered UPDATEs (the old current off, the new one on), audited.
  False when it is already current; `LookupError` when no row (or more than one) matches.

This module takes plain fields: it never imports the marketplace client.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models.platform import ProductTypeDefinition
from app.db.platform_loads import _assume_migrator, _hold_migration_lock

ENTITY: Final = "product_type_definition"
STORED: Final = "product_type_definition.stored"
PROMOTED: Final = "product_type_definition.promoted"
AUDIT_SQL: Final = (
    "SELECT audit(NULL, 'staff', NULL, :action, 'product_type_definition', :id, NULL, "
    "CAST(:after AS jsonb), :reason, :request_id, NULL, NULL, :email)"
)

Outcome = Literal["unchanged", "stored", "stored_current"]


@dataclass(frozen=True)
class DefinitionRow:
    """What `templates refresh` stores (the fields of a fetched definition)."""

    marketplace_id: str
    product_type: str
    requirements: str
    locale: str
    product_type_version: str
    requirements_enforced: str
    display_name: str | None
    property_groups: Mapping[str, Any]
    meta_schema_id: str | None
    schema: Mapping[str, Any]
    digest: bytes

    def __repr__(self) -> str:
        return f"DefinitionRow({self.product_type} {self.product_type_version})"


@dataclass(frozen=True)
class StoreResult:
    outcome: Outcome
    id: UUID
    version: str


@dataclass(frozen=True)
class Actor:
    staff_email: str
    reason: str
    request_id: str


def _key(row: DefinitionRow) -> dict[str, str]:
    return {
        "marketplace_id": row.marketplace_id,
        "product_type": row.product_type,
        "requirements": row.requirements,
        "locale": row.locale,
    }


async def _audit(
    connection: AsyncConnection, action: str, row_id: UUID, after: Mapping[str, str], actor: Actor
) -> None:
    await connection.execute(
        text(AUDIT_SQL),
        {
            "action": action,
            "id": row_id,
            "after": json.dumps(dict(after)),
            "reason": actor.reason,
            "request_id": actor.request_id,
            "email": actor.staff_email,
        },
    )


async def store_rows(connection: AsyncConnection, row: DefinitionRow, actor: Actor) -> StoreResult:
    """The store on an open transaction (`store_definition` opens one)."""
    await connection.run_sync(_assume_migrator)
    await _hold_migration_lock(connection)
    d = ProductTypeDefinition
    key = _key(row)
    same = (
        await connection.execute(
            select(d.id).where(
                d.marketplace_id == row.marketplace_id,
                d.product_type == row.product_type,
                d.requirements == row.requirements,
                d.locale == row.locale,
                d.product_type_version == row.product_type_version,
                d.schema_digest == row.digest,
            )
        )
    ).scalar_one_or_none()
    if same is not None:
        return StoreResult("unchanged", same, row.product_type_version)
    current = (
        await connection.execute(
            select(d.id).where(
                d.marketplace_id == row.marketplace_id,
                d.product_type == row.product_type,
                d.requirements == row.requirements,
                d.locale == row.locale,
                d.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    make_current = current is None
    inserted = (
        await connection.execute(
            insert(ProductTypeDefinition)
            .values(
                marketplace="amazon",
                **key,
                product_type_version=row.product_type_version,
                requirements_enforced=row.requirements_enforced,
                display_name=row.display_name,
                property_groups=dict(row.property_groups),
                schema_json=dict(row.schema),
                meta_schema_id=row.meta_schema_id,
                schema_digest=row.digest,
                fetched_at=datetime.now(UTC),
                is_current=make_current,
            )
            .returning(ProductTypeDefinition.id)
        )
    ).scalar_one()
    after = {**key, "product_type_version": row.product_type_version}
    await _audit(connection, STORED, inserted, after, actor)
    if make_current:
        await _audit(connection, PROMOTED, inserted, after, actor)
        return StoreResult("stored_current", inserted, row.product_type_version)
    return StoreResult("stored", inserted, row.product_type_version)


async def store_definition(engine: AsyncEngine, row: DefinitionRow, actor: Actor) -> StoreResult:
    async with engine.begin() as connection:
        return await store_rows(connection, row, actor)


async def promote_rows(
    connection: AsyncConnection,
    *,
    key: Mapping[str, str],
    version: str,
    digest_prefix: str | None,
    actor: Actor,
) -> bool:
    await connection.run_sync(_assume_migrator)
    await _hold_migration_lock(connection)
    d = ProductTypeDefinition
    where = [
        d.marketplace_id == key["marketplace_id"],
        d.product_type == key["product_type"],
        d.requirements == key["requirements"],
        d.locale == key["locale"],
    ]
    found = (
        await connection.execute(
            select(d.id, d.is_current, d.schema_digest).where(
                *where, d.product_type_version == version
            )
        )
    ).all()
    if digest_prefix is not None:
        prefix = digest_prefix.lower()
        found = [r for r in found if bytes(r.schema_digest).hex().startswith(prefix)]
    if len(found) != 1:
        raise LookupError("no single definition matches")
    (target,) = found
    if target.is_current:
        return False
    await connection.execute(
        update(d).where(*where, d.is_current.is_(True)).values(is_current=False)
    )
    await connection.execute(update(d).where(d.id == target.id).values(is_current=True))
    await _audit(connection, PROMOTED, target.id, {**key, "product_type_version": version}, actor)
    return True


async def promote_definition(
    engine: AsyncEngine,
    *,
    key: Mapping[str, str],
    version: str,
    digest_prefix: str | None,
    actor: Actor,
) -> bool:
    async with engine.begin() as connection:
        return await promote_rows(
            connection, key=key, version=version, digest_prefix=digest_prefix, actor=actor
        )
