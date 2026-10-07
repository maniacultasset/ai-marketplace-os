"""The transaction boundaries services open (BACKEND-ARCHITECTURE.md §3: services own the
transaction; the `services-never-import-the-engine` import contract keeps `app.db.session` out of
`app.services`, so services reach these through the repository layer).

- `unit_of_work(tenant_id, user_id=...)`: one tenant transaction (S9).
- `identity_session()` and `set_gucs()`: the public auth flows, which learn their tenant on the
  way (CODE-PLAN S10 Q10).
- `row_savepoint(session)`: one import row's writes in a SAVEPOINT (S13 B9): a database refusal
  rolls back that row only and surfaces as `RowDbError` (the SQLSTATE and the constraint name,
  never the driver message), so the batch goes on.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy.exc import DBAPIError

from app.db.session import identity_session, set_gucs, unit_of_work

__all__ = ["RowDbError", "identity_session", "row_savepoint", "set_gucs", "unit_of_work"]

_SQLSTATE: Final = re.compile(r"[0-9A-Z]{5}")
_NAME: Final = re.compile(r"[A-Za-z0-9_]{1,63}")


class RowDbError(Exception):
    """The database refused one row's writes. `sqlstate` and `constraint` are safe identifiers
    (or None); the driver message, which can echo values, is dropped."""

    def __init__(self, sqlstate: str | None, constraint: str | None) -> None:
        super().__init__(
            f"row refused (SQLSTATE {sqlstate or '?'}, constraint {constraint or '-'})"
        )
        self.sqlstate = sqlstate
        self.constraint = constraint


def _row_error(exc: DBAPIError) -> RowDbError:
    sqlstate = getattr(exc.orig, "sqlstate", None)
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return RowDbError(
        sqlstate if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate) else None,
        name if isinstance(name, str) and _NAME.fullmatch(name) else None,
    )


@asynccontextmanager
async def row_savepoint(session: Any) -> AsyncIterator[None]:
    """`async with row_savepoint(session): ...` — the block's writes commit with the outer
    transaction, or roll back alone with a `RowDbError`."""
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except DBAPIError as exc:
        raise _row_error(exc) from None
