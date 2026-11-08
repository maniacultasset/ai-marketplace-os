"""SI9 T11 (CP-D15): the listings a photo reject re-validates are locked `FOR UPDATE SKIP LOCKED`,
so a listing another transaction holds is skipped, never waited on (no database: the statement
is compiled for PostgreSQL)."""

from __future__ import annotations

import uuid
from typing import Any

from app.repositories.listings import ListingRepository
from sqlalchemy.dialects import postgresql

DIALECT: Any = postgresql.dialect()  # type: ignore[no-untyped-call]


class _Result:
    def scalars(self) -> list[Any]:
        return []


class _Session:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> _Result:
        self.statements.append(stmt)
        return _Result()


async def test_reviewable_for_product_skips_locked_rows() -> None:
    session = _Session()
    repo = ListingRepository(session, uuid.uuid4())  # type: ignore[arg-type]
    assert await repo.reviewable_for_product(uuid.uuid4()) == []
    (stmt,) = session.statements
    compiled: Any = stmt.compile
    sql = str(compiled(dialect=DIALECT)).upper()
    assert "FOR UPDATE SKIP LOCKED" in sql
    for status in ("VALIDATED", "NEEDS_REVIEW", "APPROVED"):
        assert (
            status in str(compiled(dialect=DIALECT, compile_kwargs={"literal_binds": True})).upper()
        )
    assert "PAUSED_AT IS NULL" in sql and "ARCHIVED_AT IS NULL" in sql
