"""`TenantScopedRepository.list` bounds and `_scoped_read` / `_criterion_for` (S13 A3, S23
analytics) without a database (S26 T23 gate-fix; AC 11): the statement each builds, the limit it
sends and the refusal of a tenant table without this instance's criterion. The integration and
tenant-isolation suites run the same reads against PostgreSQL."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.guard import SCOPE_OK, CrossTenantQueryError
from app.db.models.ops import ImportRow, JobRun
from app.repositories.base import LIST_LIMIT_MAX, LIST_LIMIT_MIN
from app.repositories.jobs import JobRunRepository
from sqlalchemy import func, literal, select


class _Result:
    def scalars(self) -> list[Any]:
        return ["row"]


class _Session:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result()


def _repo(session: Any = None) -> JobRunRepository:
    db: Any = session if session is not None else object()
    return JobRunRepository(db, uuid.UUID(int=3))


def _limit(stmt: Any) -> int:
    return int(stmt._limit_clause.value)


@pytest.mark.parametrize(
    ("kwargs", "limit"),
    [({}, 100), ({"limit": LIST_LIMIT_MIN}, 1), ({"limit": LIST_LIMIT_MAX}, 500)],
)
async def test_list_sends_the_limit(kwargs: dict[str, int], limit: int) -> None:
    session = _Session()
    rows: list[Any] = await _repo(session).list(**kwargs)
    assert rows == ["row"]
    (stmt,) = session.statements
    assert _limit(stmt) == limit
    assert stmt.get_execution_options()[SCOPE_OK] is True


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        (True, "limit must be an int"),
        ("5", "limit must be an int"),
        (0, "limit must be in 1..500"),
        (501, "limit must be in 1..500"),
    ],
)
async def test_list_refuses_a_bad_limit(limit: Any, message: str) -> None:
    session = _Session()
    with pytest.raises(ValueError) as refused:
        await _repo(session).list(limit=limit)
    assert str(refused.value) == message
    assert session.statements == []


def test_criterion_for_is_the_model_tenant_column_and_is_recorded() -> None:
    repo = _repo()
    criterion = repo._criterion_for(ImportRow)
    assert criterion.compare(ImportRow.tenant_col() == uuid.UUID(int=3))
    assert repo._criteria[-1] is criterion


def test_scoped_read_stamps_a_read_covered_for_every_tenant_table() -> None:
    repo = _repo()
    stmt = (
        select(func.count(JobRun.id))
        .join(ImportRow, ImportRow.id == JobRun.id)
        .where(repo._criterion_for(JobRun), repo._criterion_for(ImportRow))
    )
    stamped = repo._scoped_read(stmt)
    assert stamped.get_execution_options()[SCOPE_OK] is True
    plain = repo._scoped_read(select(literal(1)))
    assert plain.get_execution_options()[SCOPE_OK] is True


def test_scoped_read_accepts_the_tenant_criterion_too() -> None:
    repo = _repo()
    stmt = select(JobRun.id).where(repo._tenant_criterion(include_platform=False))
    assert repo._scoped_read(stmt).get_execution_options()[SCOPE_OK] is True


def test_scoped_read_refuses_a_tenant_table_without_its_criterion() -> None:
    repo = _repo()
    stmt = (
        select(JobRun.id)
        .join(ImportRow, ImportRow.id == JobRun.id)
        .where(repo._criterion_for(JobRun), ImportRow.tenant_col() == uuid.UUID(int=3))
    )
    with pytest.raises(CrossTenantQueryError) as refused:
        repo._scoped_read(stmt)
    assert str(refused.value) == (
        "read names tenant table(s) ['import_rows'] without this repository's tenant criterion"
    )


def test_scoped_read_refuses_another_instance_criterion() -> None:
    repo, other = _repo(), _repo()
    stmt = select(JobRun.id).where(other._criterion_for(JobRun))
    with pytest.raises(CrossTenantQueryError, match=r"\['job_runs'\]"):
        repo._scoped_read(stmt)


def test_scoped_read_names_every_uncovered_table_sorted() -> None:
    repo = _repo()
    stmt = select(JobRun.id).join(ImportRow, ImportRow.id == JobRun.id)
    with pytest.raises(CrossTenantQueryError, match=r"\['import_rows', 'job_runs'\]"):
        repo._scoped_read(stmt)
