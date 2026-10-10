"""app/db/guard.py without a database (S26 T23 gate-fix; AC 11): the table walk, the INSERT
parts it reads, and `check_scope` / `check_flush` over stub execution states and sessions, each
refusal with its exact message. tests/integration/test_guard.py runs the same guard on a real
AppSession."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.db.guard import (
    SCOPE_OK,
    TENANT_INFO_KEY,
    CrossTenantQueryError,
    check_flush,
    check_scope,
    insert_read_tables,
    referenced_tables,
    tenant_tables,
)
from app.db.models.ops import ImportRow, JobRun
from sqlalchemy import column, delete, exists, insert, select, table, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

TENANT = uuid.UUID(int=5)


def test_tenant_tables_names_the_tenant_scoped_tables_only() -> None:
    names = tenant_tables()
    assert {"job_runs", "import_rows", "tenants", "rules"} <= names
    assert "alembic_version" not in names


def test_referenced_tables_by_from_by_column_and_by_lightweight_name() -> None:
    assert referenced_tables(select(JobRun.id)) == {"job_runs"}
    assert referenced_tables(select(JobRun.id).order_by(ImportRow.id)) >= {"import_rows"}
    assert referenced_tables(select(column("x")).select_from(table("import_rows"))) == {
        "import_rows"
    }
    assert referenced_tables(select(column("x"))) == set()


def test_insert_read_tables_ignores_the_bare_target() -> None:
    assert insert_read_tables(insert(JobRun).values(id=uuid.uuid4())) == set()


def test_insert_read_tables_sees_every_nested_select() -> None:
    from_select = insert(JobRun).from_select(["id"], select(ImportRow.id))
    assert insert_read_tables(from_select) == {"import_rows"}
    scalar = insert(JobRun).values(id=select(ImportRow.id).limit(1).scalar_subquery())
    assert insert_read_tables(scalar) == {"import_rows"}
    multi = insert(JobRun).values(
        [{"id": uuid.uuid4()}, {"id": select(ImportRow.id).limit(1).scalar_subquery()}]
    )
    assert insert_read_tables(multi) == {"import_rows"}
    flag = exists().where(ImportRow.id == JobRun.id)
    assert "import_rows" in insert_read_tables(insert(JobRun).values(node=flag))


def test_insert_read_tables_sees_on_conflict_set_and_where() -> None:
    sub = select(ImportRow.sku).limit(1).scalar_subquery()
    base = pg_insert(JobRun).values(id=uuid.uuid4())
    on_set = base.on_conflict_do_update(index_elements=["id"], set_={"node": sub})
    assert insert_read_tables(on_set) == {"import_rows"}
    on_where = base.on_conflict_do_update(
        index_elements=["id"],
        set_={"node": "x"},
        where=exists().where(ImportRow.id == JobRun.id),
    )
    assert "import_rows" in insert_read_tables(on_where)
    target_where = base.on_conflict_do_update(
        index_elements=["id"],
        index_where=exists().where(ImportRow.id == JobRun.id),
        set_={"node": "x"},
    )
    assert "import_rows" in insert_read_tables(target_where)
    nothing = base.on_conflict_do_nothing()
    assert insert_read_tables(nothing) == set()


def _state(statement: Any, *, kind: str = "select", **over: Any) -> Any:
    state = SimpleNamespace(
        execution_options={},
        session=SimpleNamespace(info={}),
        is_column_load=False,
        is_insert=kind == "insert",
        is_select=kind == "select",
        is_update=kind == "update",
        is_delete=kind == "delete",
        statement=statement,
        all_mappers=[SimpleNamespace(class_=ImportRow), SimpleNamespace(class_=JobRun)],
    )
    for key, value in over.items():
        setattr(state, key, value)
    return state


@pytest.mark.parametrize(
    ("kind", "statement"),
    [
        ("select", select(JobRun.id)),
        ("update", update(JobRun).values(node="x")),
        ("delete", delete(JobRun)),
        ("insert", insert(JobRun).from_select(["id"], select(JobRun.id))),
    ],
)
def test_check_scope_refuses_an_unscoped_statement(kind: str, statement: Any) -> None:
    with pytest.raises(CrossTenantQueryError) as refused:
        check_scope(_state(statement, kind=kind))
    assert str(refused.value) == (
        "unscoped statement on tenant table(s) ['job_runs'] (mappers ['ImportRow', 'JobRun']); "
        "go through a TenantScopedRepository"
    )


def test_check_scope_lets_the_scoped_ones_through() -> None:
    stmt = select(JobRun.id)
    check_scope(_state(stmt, execution_options={SCOPE_OK: True}))
    check_scope(_state(stmt, session=SimpleNamespace(info={SCOPE_OK: True})))
    check_scope(_state(stmt, is_column_load=True))
    check_scope(_state(insert(JobRun).values(id=uuid.uuid4()), kind="insert"))
    check_scope(_state(select(column("x")).select_from(table("not_a_tenant_table"))))
    # neither a read nor a write (a DDL-like or text execution) is not checked
    check_scope(_state(stmt, kind="other"))


def _session(new: Any = (), dirty: Any = (), deleted: Any = (), tenant: Any = TENANT) -> Any:
    info = {} if tenant is None else {TENANT_INFO_KEY: tenant}
    return SimpleNamespace(info=info, new=list(new), dirty=list(dirty), deleted=list(deleted))


def _row(tenant: uuid.UUID = TENANT) -> JobRun:
    return JobRun(id=uuid.uuid4(), tenant_id=tenant)


@pytest.mark.parametrize(("slot", "label"), [("new", "insert"), ("deleted", "delete")])
def test_check_flush_refuses_another_tenant(slot: str, label: str) -> None:
    with pytest.raises(CrossTenantQueryError) as refused:
        check_flush(_session(**{slot: [_row(uuid.UUID(int=6))]}), None, None)
    assert str(refused.value) == f"{label} of JobRun for another tenant"


def test_check_flush_refuses_outside_a_tenant_unit_of_work() -> None:
    with pytest.raises(CrossTenantQueryError) as refused:
        check_flush(_session(new=[_row()], tenant=None), None, None)
    assert str(refused.value) == "insert of JobRun outside a tenant unit of work"


def test_check_flush_passes_own_tenant_and_non_tenant_objects() -> None:
    check_flush(_session(new=[_row(), object()], dirty=[object()], deleted=[_row()]), None, None)
    check_flush(_session(new=[object()], tenant=None), None, None)
