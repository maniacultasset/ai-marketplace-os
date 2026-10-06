"""The 0004 guard SQLSTATE map (DATABASE.md §5.1-§5.7 guards): each SQLSTATE a guard raises maps
to one ErrorCode by code alone (LST02 also by its constraint), never by the driver message."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core.errors import ConflictError
from app.db.integrity import GUARD_SQLSTATES, map_db_error
from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import DBAPIError

SQL_DIR = Path(__file__).resolve().parents[2] / "alembic" / "sql" / "0004"
EXPECTED = {
    "MKC01": ErrorCode.MAKER_CHECKER_VIOLATION,
    "OWN01": ErrorCode.LAST_OWNER_REQUIRED,
    "LST01": ErrorCode.INVALID_STATUS_TRANSITION,
    "REV01": ErrorCode.BULK_APPROVE_REQUIRES_VALIDATED,
    "SUB01": ErrorCode.DUPLICATE_SUBMISSION_BLOCKED,
    "SUB02": ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED,
    "SUB03": ErrorCode.INTERNAL_ERROR,
    "SUB04": ErrorCode.FACTS_CHANGED_SINCE_APPROVAL,
    "LIT01": ErrorCode.SELLER_SKU_CONFLICT,
    "VAR01": ErrorCode.VARIATION_INVALID,
    "VAR02": ErrorCode.VARIANT_MATRIX_INCOMPLETE,
    "GRD01": ErrorCode.INTERNAL_ERROR,
}


class _OrigError(Exception):
    def __init__(self, sqlstate: str | None, constraint: str | None = None) -> None:
        super().__init__("a message naming row values: leak_s10")
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint)


def _error(sqlstate: str | None, constraint: str | None = None) -> DBAPIError:
    return DBAPIError("UPDATE ...", {"x": "leak_s10"}, _OrigError(sqlstate, constraint))


@pytest.mark.parametrize("sqlstate", sorted(EXPECTED))
def test_each_guard_sqlstate_maps(sqlstate: str) -> None:
    mapped = map_db_error(_error(sqlstate))
    assert isinstance(mapped, ConflictError)
    assert mapped.code is EXPECTED[sqlstate]
    assert "leak_s10" not in str(mapped)


@pytest.mark.parametrize(
    ("constraint", "code"),
    [
        ("paused", ErrorCode.LISTING_PAUSED),
        ("archived", ErrorCode.LISTING_ARCHIVED),
        (None, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_lst02_maps_by_constraint(constraint: str | None, code: ErrorCode) -> None:
    mapped = map_db_error(_error("LST02", constraint))
    assert isinstance(mapped, ConflictError) and mapped.code is code


def test_unknown_is_internal() -> None:
    mapped = map_db_error(_error("ZZZ99"))
    assert isinstance(mapped, ConflictError) and mapped.code is ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize("sqlstate", ["23505", "40001", "57014", None, "bad"])
def test_standard_sqlstates_are_not_guards(sqlstate: str | None) -> None:
    assert map_db_error(_error(sqlstate)) is None


def test_every_raised_sqlstate_mapped() -> None:
    raised = set()
    for path in sorted(SQL_DIR.glob("*.up.sql")):
        raised |= set(re.findall(r"ERRCODE = '([0-9A-Z]{5})'", path.read_text(encoding="utf-8")))
    assert raised, "no ERRCODE found in alembic/sql/0004"
    assert raised <= set(GUARD_SQLSTATES) | {"LST02"}
    assert set(GUARD_SQLSTATES) == set(EXPECTED) | {"LST02"}
