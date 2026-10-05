"""What `alembic/env.py` and `app.cli` share: the role switch, the migration lock, the
newer-database check, the version-table revoke and the autogenerate render hook.

- Migrations run as `migrator`, the schema owner (CANON.md:75). Locally and in CI Alembic logs in
  as `postgres`, so `assume_migration_role` switches with a session-level `SET ROLE`; logged in as
  `migrator` (the Cloud Run job) it issues nothing.
- One session advisory lock serialises migrations, and `lock_timeout` bounds the wait
  (EXECUTION-PLAN.md:300-302; DATABASE.md:1813). The version check runs after the lock, on the
  same connection, so an overlapping deploy cannot slip a newer revision in between.
- Errors never carry connection detail: a revision id from the database passes through
  `safe_revision` before it reaches a message.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

from alembic.autogenerate.api import AutogenContext
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

# b"mkosmigr" as a big-endian bigint. It lies outside int4, so no `hashtext(...)` key (the other
# advisory locks, DATABASE.md:257 and :998) can ever equal it.
MIGRATION_LOCK_KEY: Final[int] = 7884518114189731698
LOCK_TIMEOUT: Final[str] = "5s"
LOCK_NOT_AVAILABLE: Final[str] = "55P03"

MIGRATION_ROLE: Final[str] = "migrator"
APP_ROLES: Final[tuple[str, ...]] = ("app_rw", "app_ro", "app_platform_ro")

ALEMBIC_INI: Final[Path] = Path(__file__).resolve().parents[2] / "alembic.ini"

INVALID_REVISION: Final[str] = "<invalid revision id>"
# alembic_version.version_num is VARCHAR(32).
_REVISION_ID: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z_]{1,32}")
_VECTOR_IMPORT: Final[str] = "from pgvector.sqlalchemy import Vector"


class MigrationLockBusy(RuntimeError):  # noqa: N818 - a state, not a fault; exit 4 in app.cli
    """Another migration held the lock past `lock_timeout`; nothing was applied."""

    def __init__(self) -> None:
        super().__init__("another migration holds the migration lock")


class DatabaseAheadError(RuntimeError):
    """The database records a revision this code does not contain."""

    def __init__(self, revision: str) -> None:
        self.revision = revision
        super().__init__(f"database is at revision {revision}, which this code does not contain")


def safe_revision(value: str) -> str:
    """The value when it looks like an Alembic revision id, else a fixed placeholder."""
    return value if _REVISION_ID.fullmatch(value) else INVALID_REVISION


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def assume_migration_role(connection: Connection) -> None:
    current = connection.execute(text("SELECT current_user")).scalar_one()
    if current != MIGRATION_ROLE:
        connection.execute(text(f"SET ROLE {MIGRATION_ROLE}"))


def acquire_migration_lock(connection: Connection) -> None:
    """A session advisory lock, released when the connection closes. There is no explicit unlock:
    after a failed migration the transaction is aborted, and an unlock would mask the error."""
    connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
    try:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
            raise MigrationLockBusy() from exc
        raise


def refuse_if_ahead(connection: Connection, script: ScriptDirectory) -> None:
    """Raise `DatabaseAheadError` when a revision recorded in the database is unknown here.

    Exact ids only: `ScriptDirectory.get_revision` would also resolve a prefix (`0001` to
    `0001_core_tenancy`) or the symbols `head`/`heads`, and the run would then skip as "ok"."""
    known = {revision.revision for revision in script.walk_revisions()}
    for head in MigrationContext.configure(connection).get_current_heads():
        if head not in known:
            raise DatabaseAheadError(safe_revision(head))


def revoke_version_table_privileges(connection: Connection) -> None:
    """`alembic_version` is migration state: the app roles get nothing on it, whatever the
    default privileges grant on new tables. An empty `versions/` creates no table."""
    exists = connection.execute(text("SELECT to_regclass('alembic_version')")).scalar_one()
    if exists is not None:
        roles = ", ".join((*APP_ROLES, "PUBLIC"))
        connection.execute(text(f"REVOKE ALL ON TABLE alembic_version FROM {roles}"))


def render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """Autogenerate hook: write `Vector(<dim>)` with its import, instead of pgvector's internal
    class path and no import (the revision would then fail at runtime)."""
    if type_ == "type" and isinstance(obj, VECTOR):
        autogen_context.imports.add(_VECTOR_IMPORT)
        return f"Vector({obj.dim})" if obj.dim is not None else "Vector()"
    return False
