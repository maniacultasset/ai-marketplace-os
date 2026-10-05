"""The Alembic environment: sync psycopg 3 (CANON.md:20), run as `migrator` (CANON.md:75).

The URL comes only from `ALEMBIC_DATABASE_URL` through `app.core.config`. Online, everything runs
on one connection, in this order: switch to `migrator`; take the migration lock (5 s
`lock_timeout`); refuse a database that records a revision this code lacks; commit, so the role
and the lock are session state before any per-migration transaction; run the migrations, each in
its own transaction; then revoke the app roles' privileges on `alembic_version` and commit.
Logging is configured by `python -m app.cli`, never here: `fileConfig` would replace the root
handlers of whatever process imports this file.
"""

from __future__ import annotations

from alembic import context
from alembic.script import ScriptDirectory
from app.core.config import get_settings
from app.db.base import metadata as target_metadata
from app.db.migrations import (
    acquire_migration_lock,
    assume_migration_role,
    refuse_if_ahead,
    render_item,
    revoke_version_table_privileges,
)
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def _url() -> str:
    url = get_settings().alembic_database_url
    if url is None:
        raise RuntimeError("ALEMBIC_DATABASE_URL is required to run migrations")
    return url


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
            with context.begin_transaction():
                context.run_migrations()
            revoke_version_table_privileges(connection)
            connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
