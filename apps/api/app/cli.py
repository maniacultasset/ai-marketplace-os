"""The operator command line: `python -m app.cli db migrate` (EXECUTION-PLAN.md:300-305) and
`python -m app.cli seed --profile {demo|isolation} [--reset]` (CANON-ADDENDUM.md A§5).

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

`seed` runs only under `APP_ENV=local` (PLAN D9): anything else is refused before any connection
opens. It only orchestrates; every seed write lives in `app/db/seed_ops.py` (repo rule R4).

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| seeded                               | stdout | 0    |
| any failure                          | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| refused: APP_ENV is not local        | stderr | 3    |
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
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
EXIT_REFUSED: Final = 3
SEED_PROFILES: Final = ("demo", "isolation")
RULES_SEED_PATH: Final = (
    Path(__file__).resolve().parents[3] / "packages/schemas/fixtures/rules.seed.json"
)
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


@dataclass(frozen=True)
class SeedCounts:
    tenants: int
    products: int
    users: int


async def _seed(profile: str, *, reset: bool, admin_url: str) -> SeedCounts:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import seed_ops
    from app.db.session import dispose_engine, unit_of_work

    admin = create_async_engine(admin_url)
    try:
        if reset:
            async with admin.begin() as connection:
                await seed_ops.reset_seed_tenants(connection)
        async with admin.begin() as connection:
            await seed_ops.seed_platform_rows(connection, RULES_SEED_PATH)
    finally:
        await admin.dispose()
    products = users = 0
    try:
        for tenant in seed_ops.TENANTS:
            async with unit_of_work(seed_ops.seed_tenant_id(tenant.slug)) as session:
                await seed_ops.seed_tenant(session, tenant)
                users += await seed_ops.seed_users_and_memberships(session, tenant)
                products += await seed_ops.seed_products(session, tenant, profile)
                if profile == "demo":
                    await seed_ops.seed_brand_data(session, tenant)
    finally:
        await dispose_engine()
    return SeedCounts(len(seed_ops.TENANTS), products, users)


async def run_seed(profile: str, *, reset: bool) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.app_env != "local":  # PLAN D9: the single gate, before any engine exists
        sys.stderr.write(
            f"seed: refused: APP_ENV={settings.app_env} (seed runs only under APP_ENV=local)\n"
        )
        return EXIT_REFUSED
    if settings.alembic_database_url is None or settings.database_url is None:
        sys.stderr.write("seed: failed (DATABASE_URL and ALEMBIC_DATABASE_URL are required)\n")
        return EXIT_FAILED
    try:
        counts = await _seed(profile, reset=reset, admin_url=settings.alembic_database_url)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"seed: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    sys.stdout.write(
        f"seed: ok (profile {profile}, {counts.tenants} tenants, {counts.products} products, "
        f"{counts.users} users)\n"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    groups = parser.add_subparsers(dest="group", required=True)
    db = groups.add_parser("db", help="database operations")
    db_commands = db.add_subparsers(dest="command", required=True)
    db_commands.add_parser("migrate", help="alembic upgrade head, as migrator, under the lock")
    seed = groups.add_parser("seed", help="local demo or isolation data (APP_ENV=local only)")
    seed.add_argument("--profile", required=True, choices=SEED_PROFILES)
    seed.add_argument("--reset", action="store_true", help="delete the seed tenants first")
    args = parser.parse_args(argv)
    if args.group == "seed":
        return asyncio.run(run_seed(args.profile, reset=args.reset))
    return run_migrate(alembic_config())


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    raise SystemExit(main())
