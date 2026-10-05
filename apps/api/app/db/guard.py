"""The tenant guard (S9 T4; CANON-ADDENDUM.md A§6; DATABASE.md §4 layer 2).

`check_scope` runs on every ORM execution of an `AppSession` (`do_orm_execute`). A statement that
reads, updates or deletes any `TenantScoped` table, anywhere in its tree (joins, subqueries,
EXISTS, CTEs, `select_from`, Core `Model.__table__`, a lightweight `sqlalchemy.table("name")`
naming the table), raises `CrossTenantQueryError` unless it
carries the `SCOPE_OK` execution option (stamped only by `TenantScopedRepository`) or runs in a
session whose `info[SCOPE_OK]` is set (written only by `platform_scope()`).

`check_flush` runs before every flush (D18): ORM unit-of-work writes never reach
`do_orm_execute`, so every TenantScoped object being inserted, changed or deleted must belong to
the session's `info["tenant_id"]`, and its tenant column must not change. Core INSERTs are
checked only for their source: `insert().from_select(select(...))` reads like any SELECT, so a
TenantScoped table in that select (the insert target included) needs the same scope.

Error messages name tables and mappers only, never statement parameters.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from sqlalchemy import Table
from sqlalchemy.orm import ORMExecuteState, Session, attributes
from sqlalchemy.sql import visitors
from sqlalchemy.sql.expression import TableClause

SCOPE_OK: Final = "tenant_scope_checked"
TENANT_INFO_KEY: Final = "tenant_id"


class CrossTenantQueryError(RuntimeError):
    """A statement or flush that is not scoped to the session's tenant."""


@lru_cache(maxsize=1)
def tenant_tables() -> frozenset[str]:
    """The table names of every TenantScoped mapper (built once, after the models import)."""
    import app.db.models  # noqa: F401  (registers every mapper)
    from app.db.base import Base
    from app.db.models._tenant import TenantScoped

    return frozenset(
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScoped) and isinstance(mapper.local_table, Table)
    )


def referenced_tables(statement: object) -> set[str]:
    """Every table name the statement's tree refers to, as a FROM or through a column.

    `TableClause` covers mapped `Table`s (a subclass) and lightweight `sqlalchemy.table()`s alike;
    both are matched by name."""
    names: set[str] = set()
    for element in visitors.iterate(statement):  # type: ignore[arg-type]
        if isinstance(element, TableClause):
            names.add(element.name)
        table = getattr(element, "table", None)
        if isinstance(table, TableClause):
            names.add(table.name)
    return names


def check_scope(state: ORMExecuteState) -> None:
    if state.execution_options.get(SCOPE_OK) or state.session.info.get(SCOPE_OK):
        return
    if state.is_column_load:
        # D17: a PK-bound refresh of an identity already in this session, loaded under scope.
        return
    if state.is_insert:
        source = getattr(state.statement, "select", None)
        if source is None:
            return  # `.values()` writes: PLAN §8 residual (composite FKs, RLS in S10)
        hits = referenced_tables(source) & tenant_tables()
    elif state.is_select or state.is_update or state.is_delete:
        hits = referenced_tables(state.statement) & tenant_tables()
    else:
        return
    if hits:
        mappers = sorted(m.class_.__name__ for m in state.all_mappers)
        raise CrossTenantQueryError(
            f"unscoped statement on tenant table(s) {sorted(hits)} (mappers {mappers}); "
            "go through a TenantScopedRepository"
        )


def check_flush(session: Session, flush_context: object, instances: object) -> None:
    from app.db.models._tenant import TenantScoped

    tenant_id = session.info.get(TENANT_INFO_KEY)
    for state_set, label in (
        (session.new, "insert"),
        (session.dirty, "update"),
        (session.deleted, "delete"),
    ):
        for obj in state_set:
            if not isinstance(obj, TenantScoped):
                continue
            name = type(obj).__name__
            column = type(obj).__tenant_column__
            if tenant_id is None:
                raise CrossTenantQueryError(f"{label} of {name} outside a tenant unit of work")
            if label == "update" and attributes.get_history(obj, column).has_changes():
                raise CrossTenantQueryError(f"{label} of {name} changes its tenant column")
            if getattr(obj, column) != tenant_id:
                raise CrossTenantQueryError(f"{label} of {name} for another tenant")
