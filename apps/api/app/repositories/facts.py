"""`FactWriter`: the one writer of product facts (DATABASE.md:1102 write rule; S13 B6, Q6, Q13).

Every write merges server-side, per key, so two writers of one product never lose each other's
keys: `set_attribute` runs `attributes = attributes || jsonb_build_object(key, fact)`;
`set_typed`/`set_typed_group` set the typed column(s) and merge `provenance.<key>` in the same
statement. A re-run with the same value, normalized value and source writes nothing (and so never
bumps `updated_at`).

Before any write the key is checked against the dictionary: unknown, disabled or the wrong
`storage` → 422 `UNKNOWN_FACT_KEY`; a source outside `allowed_sources`, or `ai` on a key that is
critical or carries `critical_override` for the product's category → 422
`FACT_SOURCE_FORBIDDEN`. The 0006 `facts_guard` is the backstop: a PRV SQLSTATE that still
reaches the writer maps to the same 422 (PRV01 `FACT_SOURCE_FORBIDDEN`, PRV02 `UNKNOWN_FACT_KEY`,
PRV03 `FACT_LEVEL_INVALID`). Another tenant's (or a missing) product is a `RowCountError`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from sqlalchemy import cast, func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from app.core.errors import InputError, RowCountError
from app.db.integrity import FACT_SQLSTATES
from app.db.models.catalog import Product
from app.db.models.platform import AttributeColumn, AttributeDefinition, CategoryAttribute
from app.repositories.base import TenantScopedRepository

PRV_CODES: Final[Mapping[str, ErrorCode]] = FACT_SQLSTATES


@dataclass(frozen=True, slots=True)
class _Definition:
    key: str
    storage: str
    allowed_sources: tuple[str, ...]
    is_critical: bool


def _source(fact: Mapping[str, Any]) -> str | None:
    value = fact.get("source")
    return value if isinstance(value, str) else None


class FactWriter(TenantScopedRepository[Product]):
    model = Product

    def __init__(self, session: Any, tenant_id: uuid.UUID) -> None:
        super().__init__(session, tenant_id)
        self._definitions: dict[str, _Definition | None] = {}
        self._column_keys: dict[str, str] | None = None

    # ---- the dictionary checks ----

    async def _definition(self, key: str) -> _Definition | None:
        if key not in self._definitions:
            row = (
                await self.db.execute(
                    select(
                        AttributeDefinition.key,
                        AttributeDefinition.storage,
                        AttributeDefinition.allowed_sources,
                        AttributeDefinition.is_critical,
                    ).where(AttributeDefinition.key == key, AttributeDefinition.enabled.is_(True))
                )
            ).one_or_none()
            self._definitions[key] = (
                None
                if row is None
                else _Definition(row.key, row.storage, tuple(row.allowed_sources), row.is_critical)
            )
        return self._definitions[key]

    async def column_key(self, column: str) -> str | None:
        """The dictionary key of a typed `products` column (`attribute_columns`), or None."""
        if self._column_keys is None:
            rows = (
                await self.db.execute(
                    select(AttributeColumn.column_name, AttributeColumn.attribute_key).where(
                        AttributeColumn.table_name == "products"
                    )
                )
            ).all()
            self._column_keys = {row.column_name: row.attribute_key for row in rows}
        return self._column_keys.get(column)

    async def _override(self, product_id: uuid.UUID, key: str) -> bool:
        category = (
            await self.db.execute(
                self._select().where(Product.id == product_id).with_only_columns(Product.category)
            )
        ).scalar_one_or_none()
        if category is None:
            return False
        stmt = select(CategoryAttribute.attribute_key).where(
            CategoryAttribute.category == category,
            CategoryAttribute.attribute_key == key,
            CategoryAttribute.enabled.is_(True),
            CategoryAttribute.critical_override.is_(True),
        )
        return (await self.db.execute(stmt)).first() is not None

    async def _check(
        self, product_id: uuid.UUID, key: str, fact: Mapping[str, Any], storage: str, field: str
    ) -> None:
        definition = await self._definition(key)
        if definition is None or definition.storage != storage:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, field)
        source = _source(fact)
        if source is None or source not in definition.allowed_sources:
            raise InputError(ErrorCode.FACT_SOURCE_FORBIDDEN, field)
        if source == "ai" and (definition.is_critical or await self._override(product_id, key)):
            raise InputError(ErrorCode.FACT_SOURCE_FORBIDDEN, field)

    # ---- the writes ----

    async def _run(self, product_id: uuid.UUID, stmt: Any, field: str) -> bool:
        try:
            written = (await self.db.execute(stmt)).first() is not None
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            code = PRV_CODES.get(sqlstate) if isinstance(sqlstate, str) else None
            if code is None:
                raise
            raise InputError(code, field) from exc
        if written:
            return True
        exists = await self.db.execute(
            self._select().where(Product.id == product_id).with_only_columns(Product.id)
        )
        if exists.first() is None:
            raise RowCountError(1, 0)
        return False

    def _where(self, product_id: uuid.UUID) -> list[Any]:
        if not isinstance(product_id, uuid.UUID):
            raise ValueError("product_id must be a UUID")
        return [Product.id == product_id, self._tenant_criterion(include_platform=False)]

    async def set_attribute(self, product_id: uuid.UUID, key: str, fact: Mapping[str, Any]) -> bool:
        """Merge one `attributes` fact; True when a row was written (False: the same fact)."""
        field = f"attributes.{key}"
        await self._check(product_id, key, fact, "attributes", field)
        new = cast(literal(json.dumps(dict(fact))), JSONB)
        old = Product.attributes[key]
        changed = tuple_(old["value"], old["normalized"], old["source"]).is_distinct_from(
            tuple_(new["value"], new["normalized"], new["source"])
        )
        stmt = (
            self._scoped_write(
                update(Product)
                .where(*self._where(product_id), changed)
                .values(attributes=Product.attributes.op("||")(func.jsonb_build_object(key, new)))
            )
        ).returning(Product.id)
        return await self._run(product_id, stmt, field)

    async def set_typed(
        self, product_id: uuid.UUID, column: str, value: Any, fact: Mapping[str, Any]
    ) -> bool:
        return await self.set_typed_group(product_id, {column: value}, fact)

    async def set_typed_group(
        self, product_id: uuid.UUID, values: Mapping[str, Any], fact: Mapping[str, Any]
    ) -> bool:
        """Set typed `products` columns of ONE dictionary key together (e.g. the three package
        dimensions, which a CHECK keeps all-or-none) and merge `provenance.<key>`."""
        if not values:
            raise ValueError("set_typed_group needs at least one column")
        keys = {await self.column_key(column) for column in values}
        if None in keys or len(keys) != 1:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, f"provenance.{sorted(values)[0]}")
        key = next(iter(keys))
        assert key is not None  # noqa: S101 - narrowed above
        field = f"provenance.{key}"
        await self._check(product_id, key, fact, "column", field)
        table = Product.__table__
        new = cast(literal(json.dumps(dict(fact))), JSONB)
        columns = [table.c[name] for name in values]
        changed = tuple_(*columns, Product.provenance[key]["source"]).is_distinct_from(
            tuple_(*(literal(v, table.c[n].type) for n, v in values.items()), new["source"])
        )
        stmt = (
            self._scoped_write(
                update(Product)
                .where(*self._where(product_id), changed)
                .values(
                    {
                        **{table.c[name]: value for name, value in values.items()},
                        Product.provenance: Product.provenance.op("||")(
                            func.jsonb_build_object(key, new)
                        ),
                    }
                )
            )
        ).returning(Product.id)
        return await self._run(product_id, stmt, field)
