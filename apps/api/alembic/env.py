"""The Alembic environment: sync psycopg 3 (CANON.md:20), run as `migrator` (CANON.md:75).

The URL comes only from `ALEMBIC_DATABASE_URL` through `app.core.config`. Online, everything runs
on one connection, in this order: switch to `migrator`; take the migration lock (5 s
`lock_timeout`); refuse a database that records a revision this code lacks; commit, so the role
and the lock are session state before any per-migration transaction; run the migrations, each in
its own transaction; then revoke the app roles' privileges on `alembic_version` and commit. The
revoke also runs when a revision fails, since the revisions before it are already committed.
Logging is configured by `python -m app.cli`, never here: `fileConfig` would replace the root
handlers of whatever process imports this file.

`target_metadata` is the models' MetaData (`app.db.models` registers every table), unless the
caller puts its own in `config.attributes["target_metadata"]`: tests that drive scratch revisions
compare against an empty or probe MetaData that way.
"""

from __future__ import annotations

import logging

import app.db.models  # noqa: F401 - registers every table on the shared MetaData
from alembic import context
from alembic.script import ScriptDirectory
from app.core.config import get_settings
from app.db.base import metadata
from app.db.migrations import (
    acquire_migration_lock,
    assume_migration_role,
    refuse_if_ahead,
    render_item,
    revoke_version_table_privileges,
)
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

_log = logging.getLogger("alembic.env")

target_metadata = context.config.attributes.get("target_metadata", metadata)


def _url() -> str:
    url = get_settings().alembic_database_url
    if url is None:
        raise RuntimeError("ALEMBIC_DATABASE_URL is required to run migrations")
    return url


def _note_failed_revoke(error: BaseException, revoke_error: Exception) -> None:
    """Record, by class name only (a message can carry connection detail), that the revoke after
    a failed run failed too: the app roles may still hold privileges on alembic_version."""
    name = type(revoke_error).__name__
    detail = f"revoke on alembic_version after the failed run also failed ({name})"
    error.add_note(detail)
    _log.warning(detail)


def run_migrations_offline() -> None:
    """`--sql`: render the SQL only (squawk reads it). No role switch and no revoke."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            assume_migration_role(connection)
            acquire_migration_lock(connection)
            refuse_if_ahead(connection, ScriptDirectory.from_config(context.config))
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                transaction_per_migration=True,
                render_item=render_item,
            )
            try:
                with context.begin_transaction():
                    context.run_migrations()
            except BaseException as error:
                # Revisions before the failing one are committed, alembic_version included, so
                # the revoke still runs. The role and the lock are session state, committed
                # above, so the rollback keeps both. The original error is the one raised.
                try:
                    connection.rollback()
                    revoke_version_table_privileges(connection)
                    connection.commit()
                except Exception as revoke_error:
                    _note_failed_revoke(error, revoke_error)
                raise
            revoke_version_table_privileges(connection)
            connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
