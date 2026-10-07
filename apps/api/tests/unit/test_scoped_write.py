"""`TenantScopedRepository._scoped_write` (S13 A3): a Core UPDATE/DELETE is stamped only when it
targets the repository's table and carries one of this instance's tenant criteria."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.guard import SCOPE_OK
from app.db.models.ops import ImportRow, JobRun
from app.repositories.jobs import JobRunRepository
from sqlalchemy import delete, update


def _repo() -> JobRunRepository:
    session: Any = object()
    return JobRunRepository(session, uuid.uuid4())


def test_scoped_write_stamps_own_criterion() -> None:
    repo = _repo()
    stmt = update(JobRun).where(repo._tenant_criterion(include_platform=False)).values(node=None)
    stamped = repo._scoped_write(stmt)
    assert stamped.get_execution_options()[SCOPE_OK] is True


def test_scoped_write_refuses_missing_criterion() -> None:
    repo = _repo()
    with pytest.raises(TypeError, match="tenant criterion"):
        repo._scoped_write(update(JobRun).where(JobRun.id == uuid.uuid4()).values(node=None))


def test_scoped_write_refuses_other_instance_criterion() -> None:
    repo, other = _repo(), _repo()
    stmt = delete(JobRun).where(other._tenant_criterion(include_platform=False))
    with pytest.raises(TypeError, match="tenant criterion"):
        repo._scoped_write(stmt)


def test_scoped_write_refuses_other_table() -> None:
    repo = _repo()
    criterion = repo._tenant_criterion(include_platform=False)
    with pytest.raises(TypeError, match="table"):
        repo._scoped_write(delete(ImportRow).where(criterion))
