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

SI1 T6 adds the two clears a PATCH `null` needs: `clear_attribute` removes a seller-written
`attributes.<key>` (a Fact from a document, the PIM or a marketplace is 422: the seller never
erases evidence they did not give), and `clear_typed` NULLs every column of one dictionary key and
drops `provenance.<key>` in one statement. A repeat writes nothing. SI3: `clear_attribute` also
removes an `ai` Fact, which is how the seller rejects a value read from the photos.

SI3 adds the photo writes. `set_photo_attribute` writes an unconfirmed `ai` Fact only through a
compare-and-set on the state the key had when the generation context was built (`expected`):
still absent, or the same unconfirmed `ai` Fact with the same `updated_at`. A seller edit or a
reject committed while the pipeline ran therefore wins, with no lock across the model call.
`confirm_attribute` stamps `confirmed_by`/`confirmed_at` on a photo Fact (`image:` evidence).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from sqlalchemy import and_, cast, false, func, literal, not_, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from app.core.errors import InputError, RowCountError
from app.db.integrity import FACT_SQLSTATES
from app.db.models.catalog import Product
from app.db.models.platform import AttributeColumn, AttributeDefinition, CategoryAttribute
from app.repositories.base import TenantScopedRepository

PRV_CODES: Final[Mapping[str, ErrorCode]] = FACT_SQLSTATES
# the source a clear is checked as: only a seller clears, and only what the seller wrote
_SELLER: Final[Mapping[str, Any]] = {"source": "seller"}


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

    async def set_photo_attribute(
        self,
        product_id: uuid.UUID,
        key: str,
        fact: Mapping[str, Any],
        *,
        expected: Mapping[str, Any] | None,
    ) -> bool:
        """Write one photo value as an unconfirmed `ai` Fact (module docstring); True only when a
        row was written. `expected` is the own-row Fact the key held at context build, or None
        for "absent". `ai` on a critical or override key is 422 `FACT_SOURCE_FORBIDDEN`."""
        if fact.get("source") != "ai" or fact.get("captured_via") != "agent":
            raise ValueError("a photo fact is source 'ai', captured_via 'agent'")
        if expected is not None and (
            expected.get("source") != "ai" or expected.get("confirmed_at") is not None
        ):
            raise ValueError("only an absent key or an unconfirmed ai fact can be filled")
        field = f"attributes.{key}"
        await self._check(product_id, key, fact, "attributes", field)
        new = cast(literal(json.dumps(dict(fact))), JSONB)
        old = Product.attributes[key]
        changed = tuple_(old["value"], old["normalized"], old["source"]).is_distinct_from(
            tuple_(new["value"], new["normalized"], new["source"])
        )
        if expected is None:
            unchanged_since_build: Any = not_(Product.attributes.has_key(key))
        elif not isinstance(expected.get("updated_at"), str):
            unchanged_since_build = false()  # fail closed: nothing to compare against
        else:
            unchanged_since_build = and_(
                old["source"].astext == "ai",
                old["confirmed_at"].astext.is_(None),
                old["updated_at"].astext == expected["updated_at"],
            )
        stmt = (
            self._scoped_write(
                update(Product)
                .where(*self._where(product_id), unchanged_since_build, changed)
                .values(attributes=Product.attributes.op("||")(func.jsonb_build_object(key, new)))
            )
        ).returning(Product.id)
        return await self._run(product_id, stmt, field)

    async def confirm_attribute(
        self,
        product_id: uuid.UUID,
        key: str,
        *,
        user_id: uuid.UUID,
        at: str,
        field: str | None = None,
    ) -> bool:
        """Confirm a photo value as it is: set `confirmed_by` and `confirmed_at` together on an
        `ai` Fact with `image:` evidence. True when written; an already confirmed one is False.
        Any other stored state (none, a seller Fact, other evidence) is 422 `VALIDATION_FAILED`
        at `field` (default `attributes.<key>`)."""
        at_field = field or f"attributes.{key}"
        old = Product.attributes[key]
        stamp = func.jsonb_build_object(
            "confirmed_by", str(user_id), "confirmed_at", at, "updated_at", at
        )
        photo_fact = and_(
            old["source"].astext == "ai", old["evidence_ref"].astext.startswith("image:")
        )
        stmt = (
            self._scoped_write(
                update(Product)
                .where(*self._where(product_id), photo_fact, old["confirmed_at"].astext.is_(None))
                .values(
                    attributes=Product.attributes.op("||")(
                        func.jsonb_build_object(key, old.op("||")(stamp))
                    )
                )
            )
        ).returning(Product.id)
        if await self._run(product_id, stmt, at_field):
            return True
        confirmed = (
            await self.db.execute(
                self._select()
                .where(Product.id == product_id, photo_fact)
                .with_only_columns(old["confirmed_at"].astext.is_not(None))
            )
        ).scalar_one_or_none()
        if confirmed:
            return False
        raise InputError(ErrorCode.VALIDATION_FAILED, at_field)

    async def clear_attribute(self, product_id: uuid.UUID, key: str) -> bool:
        """Remove a seller-written or photo-derived (`ai`) `attributes.<key>`; True when a row was
        written. A Fact with another source is 422 `VALIDATION_FAILED` at `attributes.<key>`."""
        field = f"attributes.{key}"
        await self._check(product_id, key, _SELLER, "attributes", field)
        stmt = (
            self._scoped_write(
                update(Product)
                .where(
                    *self._where(product_id),
                    Product.attributes[key]["source"].astext.in_(("seller", "ai")),
                )
                .values(attributes=Product.attributes.op("-")(key))
            )
        ).returning(Product.id)
        if await self._run(product_id, stmt, field):
            return True
        stored = (
            await self.db.execute(
                self._select()
                .where(Product.id == product_id)
                .with_only_columns(Product.attributes.has_key(key))
            )
        ).scalar_one()
        if stored:
            raise InputError(ErrorCode.VALIDATION_FAILED, field)
        return False

    async def columns_of(self, key: str) -> tuple[str, ...]:
        """The typed `products` columns of dictionary key `key`, in name order."""
        if self._column_keys is None:
            await self.column_key("")
        assert self._column_keys is not None  # noqa: S101 - filled above
        return tuple(sorted(c for c, k in self._column_keys.items() if k == key))

    async def clear_typed(self, product_id: uuid.UUID, key: str) -> bool:
        """NULL every `products` column of dictionary key `key` and drop `provenance.<key>`, in
        one statement; True when a row was written (False: nothing was set)."""
        field = f"provenance.{key}"
        await self._check(product_id, key, _SELLER, "column", field)
        names = await self.columns_of(key)
        if not names:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, field)
        table = Product.__table__
        columns = [table.c[name] for name in names]
        anything = or_(*(c.is_not(None) for c in columns), Product.provenance.has_key(key))
        stmt = (
            self._scoped_write(
                update(Product)
                .where(*self._where(product_id), anything)
                .values(
                    {
                        **dict.fromkeys(columns),
                        Product.provenance: Product.provenance.op("-")(key),
                    }
                )
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
