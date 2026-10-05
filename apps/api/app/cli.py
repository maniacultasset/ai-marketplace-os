"""The operator command line: `python -m app.cli db migrate` (EXECUTION-PLAN.md:300-305).

`db migrate` runs `alembic upgrade head` through `alembic/env.py`, which switches to `migrator`,
takes the migration lock and refuses a database newer than this image. The Cloud Run job runs it
with max-retries 0, never at container start. It prints exactly one line:

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| applied (or already at head)         | stdout | 0    |
| any other failure                    | stderr | 1    |
| the database is ahead of this image  | stderr | 3    |
| another migration holds the lock     | stderr | 4    |

A failure line names the exception class and, for a driver error, its SQLSTATE: never the
message, which can carry the DSN, the user or the host. A Settings boot error is
`get_settings()`'s own SystemExit (env-var names only) and propagates. Alembic's INFO lines go to
stdout, so stderr carries only the fixed line. Nothing imports this module (.importlinter).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError

from app.db.migrations import (
    DatabaseAheadError,
    MigrationLockBusy,
    alembic_config,
    safe_revision,
)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_DATABASE_AHEAD: Final = 3
EXIT_LOCK_BUSY: Final = 4
_SQLSTATE: Final = re.compile(r"[0-9A-Z]{5}")


def _describe(exc: Exception) -> str:
    parts = [type(exc).__name__]
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate):
            parts.append(f"SQLSTATE {sqlstate}")
    return ", ".join(parts)


def run_migrate(config: Config) -> int:
    try:
        command.upgrade(config, "head")
    except DatabaseAheadError as exc:
        sys.stderr.write(
            f"migrate: refused: the database is at revision {safe_revision(exc.revision)}, "
            "which this image does not contain\n"
        )
        return EXIT_DATABASE_AHEAD
    except MigrationLockBusy:
        sys.stderr.write("migrate: another migration holds the lock; nothing was applied\n")
        return EXIT_LOCK_BUSY
    except Exception as exc:  # every other failure: class and SQLSTATE only, never the message
        sys.stderr.write(f"migrate: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    heads = ScriptDirectory.from_config(config).get_heads()
    sys.stdout.write(f"migrate: ok (head {','.join(sorted(heads)) or 'none'})\n")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    groups = parser.add_subparsers(dest="group", required=True)
    db = groups.add_parser("db", help="database operations")
    db_commands = db.add_subparsers(dest="command", required=True)
    db_commands.add_parser("migrate", help="alembic upgrade head, as migrator, under the lock")
    parser.parse_args(argv)
    return run_migrate(alembic_config())


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    raise SystemExit(main())
