"""`TenantScopedRepository[ModelT]`, the only select factory for tenant tables (S9 T7;
invariant 5; DATABASE.md §4 layer 2).

Every statement it issues filters on the tenant column and carries the `SCOPE_OK` execution
option, which is what lets it past the guard in `app/db/guard.py`. Another tenant's row reads
as missing (`None`, D7), never as forbidden. A state-changing UPDATE must touch exactly one row
(DATABASE.md:133).
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import CTE, ColumnElement, Select, Subquery, Table, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.util import identity_key
from sqlalchemy.sql.elements import ClauseElement

from app.core.errors import RowCountError
from app.db.guard import SCOPE_OK, referenced_tables, tenant_tables
from app.db.integrity import to_conflict
from app.db.models._tenant import TenantScoped

LIST_LIMIT_MIN = 1
LIST_LIMIT_MAX = 500  # D15


def _require_uuid(name: str, value: object) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID, got {type(value).__name__}")
    return value


def _children_outside(root: ClauseElement, skip: ClauseElement) -> Iterator[ClauseElement]:
    """Every element under `root` except `skip` (or an annotated copy) and what lies under it."""
    stack: list[ClauseElement] = [root]
    seen: set[int] = set()
    while stack:
        element = stack.pop()
        if id(element) in seen or _same_selectable(element, skip):
            continue
        seen.add(id(element))
        yield element
        stack.extend(element.get_children())


def _same_selectable(element: ClauseElement, skip: ClauseElement) -> bool:
    """`skip` itself, or an ORM-annotated copy of it (`aliased(Model, cte)` annotates the CTE)."""
    if element is skip:
        return True
    return isinstance(element, (CTE, Subquery)) and element._deannotate() is skip


def _own_tables(node: ClauseElement) -> set[str]:
    if isinstance(node, Table):
        return {node.name}
    table = getattr(node, "table", None)
    return {table.name} if isinstance(table, Table) else set()


class TenantScopedRepository[ModelT: TenantScoped]:
    model: ClassVar[type[Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = getattr(cls, "model", None)
        if model is None:
            raise TypeError(f"{cls.__name__} must set `model`")
        if not (
            isinstance(model, type)
            and issubclass(model, TenantScoped)
            and isinstance(getattr(model, "__mapper__", None), Mapper)
        ):
            raise TypeError(f"{cls.__name__}.model must be a mapped TenantScoped class")

    def __init__(self, db: AsyncSession, tenant_id: UUID) -> None:
        self.db = db
        self.tenant_id = _require_uuid("tenant_id", tenant_id)
        # The tenant criteria this instance built: `_scoped_outer` accepts an inner statement
        # only if it carries one of them (object identity survives generative narrowing).
        self._criteria: builtins.list[ColumnElement[bool]] = []

    # ---- statement factories ----

    def _tenant_criterion(self, *, include_platform: bool) -> ColumnElement[bool]:
        column = self.model.tenant_col()
        if include_platform:
            if not self.model.__platform_rows__:
                raise ValueError(f"{self.model.__name__} has no platform rows")
            criterion: ColumnElement[bool] = or_(column == self.tenant_id, column.is_(None))
        else:
            criterion = column == self.tenant_id
        self._criteria.append(criterion)
        return criterion

    def _select(self, *, include_platform: bool = False) -> Select[tuple[ModelT]]:
        criterion = self._tenant_criterion(include_platform=include_platform)
        return select(self.model).where(criterion).execution_options(**{SCOPE_OK: True})

    def _scoped_outer(self, stmt: Select[Any], inner: CTE | Subquery) -> Select[Any]:
        """Stamp an outer statement built over a CTE/subquery of this instance's `_select()`.
        Refuses an inner statement this instance did not scope, an inner statement that reads
        another tenant table, and an outer statement that reads any tenant table directly."""
        element = inner.element
        where = getattr(element, "_where_criteria", ())
        if not any(mine is criterion for mine in self._criteria for criterion in where):
            raise TypeError("inner statement was not built by this repository's _select()")
        own = {self.model.__table__.name}
        if referenced_tables(element) & tenant_tables() - own:
            raise TypeError("inner statement reads another tenant table")
        if any(_own_tables(node) & tenant_tables() for node in _children_outside(stmt, inner)):
            raise TypeError("outer statement reads a tenant table outside the scoped inner")
        return stmt.execution_options(**{SCOPE_OK: True})

    # ---- reads ----

    def _id_column(self) -> Any:
        primary = inspect(self.model).primary_key
        if len(primary) != 1 or primary[0].name != "id":
            raise TypeError(f"{self.model.__name__} has no single `id` primary key")
        return self.model.id

    async def get_by_id(self, id: UUID) -> ModelT | None:
        id_column = self._id_column()
        stmt = self._select().where(id_column == _require_uuid("id", id))
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list(self, *, limit: int = 100) -> builtins.list[ModelT]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an int")
        if not LIST_LIMIT_MIN <= limit <= LIST_LIMIT_MAX:
            raise ValueError(f"limit must be in {LIST_LIMIT_MIN}..{LIST_LIMIT_MAX}")
        order = list(inspect(self.model).primary_key)
        stmt = self._select().order_by(*order).limit(limit)
        return builtins.list((await self.db.execute(stmt)).scalars())

    # ---- writes ----

    async def add(self, obj: ModelT) -> ModelT:
        if not isinstance(obj, self.model):
            raise TypeError(f"expected {self.model.__name__}, got {type(obj).__name__}")
        column = self.model.__tenant_column__
        current = getattr(obj, column)
        if current is None:
            setattr(obj, column, self.tenant_id)
        elif current != self.tenant_id:
            raise ValueError(f"{self.model.__name__} belongs to another tenant")
        self.db.add(obj)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        return obj

    async def update_one(self, id: UUID, /, **values: object) -> None:
        id_column = self._id_column()
        _require_uuid("id", id)
        if not values:
            raise ValueError("update_one needs at least one column")
        columns = {attr.key for attr in inspect(self.model).column_attrs}
        refused = {"id", self.model.__tenant_column__}
        bad = sorted(key for key in values if key in refused or key not in columns)
        if bad:
            raise ValueError(f"update_one refuses column(s) {bad}")
        criterion = self._tenant_criterion(include_platform=False)
        stmt = (
            update(self.model)
            .where(id_column == id, criterion)
            .values(**values)
            .execution_options(**{SCOPE_OK: True}, synchronize_session=False)
        )
        try:
            result = await self.db.execute(stmt)
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        rowcount = int(getattr(result, "rowcount", -1))
        if rowcount != 1:
            raise RowCountError(1, rowcount)
        loaded = self.db.identity_map.get(identity_key(self.model, id))
        if loaded is not None:
            # Server-set columns (the updated_at trigger) and the new values reload on access.
            self.db.expire(loaded)
