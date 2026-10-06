"""IntegrityError → ConflictError by constraint name only (KNOWN-ISSUES.md:50; DATABASE.md:407)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from app.core.errors import ConflictError, DomainError
from app.db.integrity import CONSTRAINT_CODES, to_conflict
from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import IntegrityError


class _OrigError(Exception):
    def __init__(self, message: str, constraint: str | None, sqlstate: str = "23505") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint)


def _error(orig: Exception) -> IntegrityError:
    return IntegrityError("INSERT ...", {"sku": "leak_s9"}, orig)


def test_sku_constraint_maps_sku_conflict() -> None:
    err = to_conflict(_error(_OrigError("dup", "products_tenant_sku_idx")))
    assert isinstance(err, ConflictError) and isinstance(err, DomainError)
    assert err.code is ErrorCode.SKU_CONFLICT
    assert err.constraint == "products_tenant_sku_idx"


def test_unmapped_constraint_internal_error() -> None:
    err = to_conflict(_error(_OrigError("dup", "some_other_idx")))
    assert err.code is ErrorCode.INTERNAL_ERROR
    assert err.constraint == "some_other_idx"


def test_missing_diag_internal_error() -> None:
    # The message names the mapped constraint, but only diag.constraint_name may decide.
    orig = _OrigError('duplicate key value violates "products_tenant_sku_idx"', None)
    err = to_conflict(_error(orig))
    assert err.code is ErrorCode.INTERNAL_ERROR
    no_diag = Exception("products_tenant_sku_idx")
    assert to_conflict(_error(no_diag)).code is ErrorCode.INTERNAL_ERROR


def test_log_has_no_values(caplog: pytest.LogCaptureFixture) -> None:
    orig = _OrigError("Key (sku)=(leak_s9) owner a.person@example.test exists", "some_other_idx")
    with caplog.at_level(logging.DEBUG):
        err = to_conflict(_error(orig))
    assert "leak_s9" not in caplog.text and "example.test" not in caplog.text
    assert "some_other_idx" in caplog.text and "23505" in caplog.text
    assert "leak_s9" not in str(err)


def test_every_mapped_constraint_exists_in_metadata() -> None:
    import app.db.models  # noqa: F401  (registers every table)
    from app.db.base import metadata

    names: set[str] = set()
    for table in metadata.tables.values():
        names |= {str(c.name) for c in table.constraints if c.name}
        names |= {str(i.name) for i in table.indexes if i.name}
    assert set(CONSTRAINT_CODES) <= names
