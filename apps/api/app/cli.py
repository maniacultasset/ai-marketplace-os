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

`dictionary sync` (S13 B5) upserts `mkos_schemas/data/attribute_dictionary.json` into the platform
dictionary tables as `migrator` (ALEMBIC_DATABASE_URL), after every `upgrade` (DATABASE.md §8.2);
an unchanged file writes no row. It prints one line: `ok (...)` on stdout and exit 0, or
`failed (<class>[, SQLSTATE])` on stderr and exit 1.

`templates load --file PATH` (S13 B7) loads a JSON list of `{version, product_type, template}`
into `category_templates` as `migrator`, in one transaction: a stored version with the same
content is a no-op, one with other content refuses the file ("bump version", exit 1, nothing
written), a new version is inserted and promoted when it is newer than the current one (audited
with a NULL tenant). An invalid file exits 1, a missing one 2.

`errors load --file PATH` (S20 T4) loads a JSON list of `marketplace_error_map` rows as `migrator`,
in one transaction: a stored key with the same content is a no-op, one that differs only in
`enabled` flips it, any other difference refuses the file ("bump version", exit 1, nothing
written); each write is audited with a NULL tenant. An invalid file exits 1, a missing one 2.

`platform load [--error-map PATH]` (S20 T5b) loads every platform row a fresh database needs and
no tenant row: the S9 platform rows (templates, NULL-tenant rules, the dictionary), the Amazon
category mappings, and the error map (default: the local fake fixture). Like `seed` it runs only
under `APP_ENV=local` (exit 3); staging and production use `templates load` and `errors load`.

`jobs dead-letter list [--tenant SLUG] [--limit 1..500]` and `jobs requeue <job_id> --staff-email E
--reason R` are the staff dead-letter tools (S13 PLAN D2; DATABASE.md:312; BACKEND-ARCHITECTURE
:990-1031): no `/api/v1` path exists for them. Each command mints its own `request_id`. The list
reads through `platform_scope()` (read-only, cross-tenant) and prints ids, kinds, attempts,
codes and times only, never payloads, reports or secrets. A requeue is one transaction in the
job's tenant: a NEW `job_runs` row (`parent_job_id` = the old one, a fresh `correlation_id`), its
queue entry, the kind's requeue hook and a `staff` audit row naming E and R.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| listed / requeued                    | stdout | 0    |
| any other failure                    | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| not eligible (unknown, not dead_letter or failed, already requeued, tenant not running) | stderr | 4 |
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import uuid
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
EXIT_NOT_ELIGIBLE: Final = 4
DEAD_LETTER_LIMIT_MAX: Final = 500
_EMAIL: Final = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}")
SEED_PROFILES: Final = ("demo", "isolation")
RULES_SEED_PATH: Final = (
    Path(__file__).resolve().parents[3] / "packages/schemas/fixtures/rules.seed.json"
)
# The local fake error map (S20 D7): `FAKE-` codes only; seed and `platform load` read it.
ERROR_MAP_FAKE_PATH: Final = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/error_map.fake.json"
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
        if profile == "demo":
            await _seed_demo_listing()
    finally:
        await dispose_engine()
    # S20 C12, C18: the one platform-load path. The Amazon mappings are promoted after the demo
    # listing, so KURTA-001 stays pinned to S9's version 1 on fresh and existing databases.
    await _platform_load(admin_url, ERROR_MAP_FAKE_PATH)
    return SeedCounts(len(seed_ops.TENANTS), products, users)


async def _platform_load(admin_url: str, error_map: Path) -> tuple[int, int, int, int]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import seed_ops
    from app.db.platform_loads import load_error_map_file

    rows = load_error_map_file(error_map)
    admin = create_async_engine(admin_url)
    try:
        counts = await seed_ops.platform_load(
            admin, rules_path=RULES_SEED_PATH, error_map_rows=rows
        )
    finally:
        await admin.dispose()
    t, e = counts.templates, counts.error_map
    return t.inserted, t.promoted, e.inserted, e.toggled


async def run_platform_load(error_map: Path) -> int:
    from pydantic import ValidationError

    from app.core.config import get_settings
    from app.db.platform_loads import ErrorMapConflict, TemplateConflict

    settings = get_settings()
    if settings.app_env != "local":  # it loads the local fake fixtures: local only, like seed
        sys.stderr.write(
            f"platform load: refused: APP_ENV={settings.app_env} (platform load runs only under "
            "APP_ENV=local; use templates load and errors load)\n"
        )
        return EXIT_REFUSED
    if settings.alembic_database_url is None:
        sys.stderr.write("platform load: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    try:
        templates, promoted, errors, toggled = await _platform_load(
            settings.alembic_database_url, error_map
        )
    except (ErrorMapConflict, TemplateConflict) as exc:
        sys.stderr.write(
            f"platform load: refused: {exc.key} already holds other content; bump version\n"
        )
        return EXIT_FAILED
    except (ValidationError, ValueError) as exc:
        sys.stderr.write(f"platform load: invalid file ({type(exc).__name__})\n")
        return EXIT_FAILED
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"platform load: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    sys.stdout.write(
        f"platform load: ok ({templates} templates inserted, {promoted} promoted, "
        f"{errors} error-map rows inserted, {toggled} toggled)\n"
    )
    return EXIT_OK


async def _seed_demo_listing() -> None:
    """S14b T19 (D12): the demo listing's rules run is made by the engine itself, from the
    seeded rules and template, then written by `seed_ops` (this module holds no SQL, R4)."""
    from mkos_schemas.listing import CanonicalListing

    from app.db import seed_ops
    from app.db.session import unit_of_work
    from app.repositories.uow import tenant_uow
    from app.services.rules_engine import evaluate
    from app.services.rules_loader import load_rules

    tenant_id = seed_ops.seed_tenant_id("acme")
    async with tenant_uow(tenant_id) as uow:
        loaded = await load_rules(
            uow,
            tenant_id=tenant_id,
            marketplace=seed_ops.DEMO_LISTING_MARKETPLACE,
            category=seed_ops.DEMO_LISTING_CATEGORY,
        )
        product_id = await seed_ops.demo_listing_product(uow.session)
    if loaded.template is None or loaded.template_id is None or loaded.template_version is None:
        raise RuntimeError("the demo listing's category template is missing")
    listing = CanonicalListing.model_validate(
        {
            "listing_id": seed_ops.demo_listing_ids()[0],
            "product_id": product_id,
            "marketplace": seed_ops.DEMO_LISTING_MARKETPLACE,
            "category": seed_ops.DEMO_LISTING_CATEGORY,
            "content": seed_ops.demo_listing_content(),
            "attributes": dict(seed_ops.DEMO_ATTRIBUTES),
        }
    )
    report = evaluate(listing, loaded.ruleset, template=loaded.template)
    async with unit_of_work(tenant_id) as session:
        await seed_ops.seed_demo_listing(
            session,
            report=report,
            rules_version=loaded.ruleset.rules_version,
            template_id=loaded.template_id,
            template_version=loaded.template_version,
        )


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


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--limit must be an integer") from None
    if not 1 <= number <= DEAD_LETTER_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"--limit must be in 1..{DEAD_LETTER_LIMIT_MAX}")
    return number


def _staff_email(value: str) -> str:
    if not _EMAIL.fullmatch(value.strip()):
        raise argparse.ArgumentTypeError("--staff-email must be an email address")
    return value.strip().lower()


def _reason(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 500:
        raise argparse.ArgumentTypeError("--reason must be 1-500 characters")
    return text


def _job_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("job_id must be a UUID") from None


async def run_dead_letter_list(tenant: str | None, limit: int) -> int:
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.jobs import list_dead_letters

    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                rows = await list_dead_letters(session, tenant_slug=tenant, limit=limit)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"jobs dead-letter list: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    for row in rows:
        finished = row.finished_at.isoformat() if row.finished_at else "-"
        sys.stdout.write(
            f"{row.job_id}\t{row.tenant_slug}\t{row.kind}\t{row.status}\t"
            f"attempt {row.attempt}\t{row.error_code or '-'}\t{finished}\n"
        )
    sys.stdout.write(f"jobs dead-letter list: {len(rows)} job(s)\n")
    return EXIT_OK


async def _requeue(job_id: uuid.UUID, staff_email: str, reason: str) -> uuid.UUID:
    from app.core.request_context import bind_tenant
    from app.db.session import platform_scope, unit_of_work
    from app.repositories.identity import AuditRepository
    from app.repositories.jobs import JobRunRepository, NotEligibleError, job_tenant
    from app.services.job_hooks import run_requeue_hook

    async with platform_scope() as session:
        tenant_id = await job_tenant(session, job_id)
    if tenant_id is None:
        raise NotEligibleError("unknown job")
    bind_tenant(tenant_id)
    async with unit_of_work(tenant_id) as session:
        old, new = await JobRunRepository(session, tenant_id).requeue(job_id)
        await run_requeue_hook(session, old, new)
        await AuditRepository(session).write(
            tenant_id=tenant_id,
            actor_type="staff",
            actor_user_id=None,
            actor_email=staff_email,
            action="job.requeue",
            entity_type="job_run",
            entity_id=new.id,
            before=json.dumps(
                {
                    "kind": old.kind,
                    "status": old.status,
                    "parent_job_id": str(old.parent_job_id) if old.parent_job_id else None,
                }
            ),
            after=json.dumps(
                {"kind": new.kind, "status": new.status, "parent_job_id": str(old.id)}
            ),
            reason=reason,
        )
    return new.id


async def run_requeue(job_id: uuid.UUID, staff_email: str, reason: str) -> int:
    from app.core.errors import ConflictError
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine
    from app.repositories.jobs import NotEligibleError

    try:
        with request_scope(new_request_id()):
            new_id = await _requeue(job_id, staff_email, reason)
    except NotEligibleError as exc:
        sys.stderr.write(f"jobs requeue: not eligible ({exc})\n")
        return EXIT_NOT_ELIGIBLE
    except ConflictError:  # a concurrent requeue won the UNIQUE (parent_job_id)
        sys.stderr.write("jobs requeue: not eligible (the job was already requeued)\n")
        return EXIT_NOT_ELIGIBLE
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"jobs requeue: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    sys.stdout.write(f"jobs requeue: ok (new job {new_id}, parent {job_id})\n")
    return EXIT_OK


async def _dictionary_sync(admin_url: str, path: Path | None) -> tuple[int, int, int]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.platform_loads import sync_dictionary

    engine = create_async_engine(admin_url)
    try:
        counts = await sync_dictionary(engine, path)
    finally:
        await engine.dispose()
    return counts.inserted, counts.updated, counts.disabled


async def run_dictionary_sync(path: Path | None) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write("dictionary sync: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    try:
        inserted, updated, disabled = await _dictionary_sync(settings.alembic_database_url, path)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"dictionary sync: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    sys.stdout.write(
        f"dictionary sync: ok ({inserted} inserted, {updated} updated, {disabled} disabled)\n"
    )
    return EXIT_OK


EXIT_NO_FILE: Final = 2


async def _templates_load(admin_url: str, path: Path) -> tuple[int, int, int]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.platform_loads import load_template_file, load_templates

    entries = load_template_file(path)
    engine = create_async_engine(admin_url)
    try:
        counts = await load_templates(engine, entries)
    finally:
        await engine.dispose()
    return counts.inserted, counts.unchanged, counts.promoted


async def run_templates_load(path: Path) -> int:
    from pydantic import ValidationError

    from app.core.config import get_settings
    from app.db.platform_loads import TemplateConflict

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write("templates load: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    try:
        inserted, unchanged, promoted = await _templates_load(settings.alembic_database_url, path)
    except TemplateConflict as exc:
        sys.stderr.write(
            f"templates load: refused: {exc.key} already holds other content; bump version "
            "(nothing was written)\n"
        )
        return EXIT_FAILED
    except (ValidationError, ValueError) as exc:
        sys.stderr.write(
            f"templates load: invalid file ({type(exc).__name__}); nothing was written\n"
        )
        return EXIT_FAILED
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"templates load: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    sys.stdout.write(
        f"templates load: ok ({inserted} inserted, {unchanged} unchanged, {promoted} promoted)\n"
    )
    return EXIT_OK


async def _errors_load(admin_url: str, path: Path) -> tuple[int, int, int]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.platform_loads import load_error_map, load_error_map_file

    rows = load_error_map_file(path)
    engine = create_async_engine(admin_url)
    try:
        counts = await load_error_map(engine, rows)
    finally:
        await engine.dispose()
    return counts.inserted, counts.unchanged, counts.toggled


async def run_errors_load(path: Path) -> int:
    from pydantic import ValidationError

    from app.core.config import get_settings
    from app.db.platform_loads import ErrorMapConflict

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write("errors load: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    try:
        inserted, unchanged, toggled = await _errors_load(settings.alembic_database_url, path)
    except ErrorMapConflict as exc:
        sys.stderr.write(
            f"errors load: refused: {exc.key} already holds other content; bump version "
            "(nothing was written)\n"
        )
        return EXIT_FAILED
    except (ValidationError, ValueError) as exc:
        sys.stderr.write(f"errors load: invalid file ({type(exc).__name__}); nothing was written\n")
        return EXIT_FAILED
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"errors load: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    sys.stdout.write(
        f"errors load: ok ({inserted} inserted, {unchanged} unchanged, {toggled} toggled)\n"
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
    jobs = groups.add_parser("jobs", help="staff dead-letter tools (S13)")
    jobs_commands = jobs.add_subparsers(dest="command", required=True)
    dead = jobs_commands.add_parser("dead-letter", help="dead-lettered jobs")
    dead_commands = dead.add_subparsers(dest="action", required=True)
    listing = dead_commands.add_parser("list", help="the newest dead letters, all tenants")
    listing.add_argument("--tenant", metavar="SLUG", default=None)
    listing.add_argument("--limit", type=_limit, default=100)
    requeue = jobs_commands.add_parser("requeue", help="a new job for a dead_letter/failed one")
    requeue.add_argument("job_id", type=_job_id)
    requeue.add_argument("--staff-email", type=_staff_email, required=True)
    requeue.add_argument("--reason", type=_reason, required=True)
    dictionary = groups.add_parser("dictionary", help="the attribute dictionary (migrator)")
    dictionary_commands = dictionary.add_subparsers(dest="command", required=True)
    sync = dictionary_commands.add_parser("sync", help="upsert attribute_dictionary.json")
    sync.add_argument("--file", type=Path, default=None, help=argparse.SUPPRESS)
    templates = groups.add_parser("templates", help="category templates (migrator)")
    templates_commands = templates.add_subparsers(dest="command", required=True)
    load = templates_commands.add_parser("load", help="load versioned category templates")
    load.add_argument("--file", type=Path, required=True)
    errors = groups.add_parser("errors", help="the marketplace error map (migrator)")
    errors_commands = errors.add_subparsers(dest="command", required=True)
    errors_load = errors_commands.add_parser("load", help="load versioned error-map rows")
    errors_load.add_argument("--file", type=Path, required=True)
    platform = groups.add_parser("platform", help="every platform row, no tenant (local only)")
    platform_commands = platform.add_subparsers(dest="command", required=True)
    platform_load = platform_commands.add_parser("load", help="templates, rules, error map")
    platform_load.add_argument("--error-map", type=Path, default=ERROR_MAP_FAKE_PATH)
    args = parser.parse_args(argv)
    if args.group == "platform":
        if not args.error_map.is_file():
            sys.stderr.write("platform load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_platform_load(args.error_map))
    if args.group == "errors":
        if not args.file.is_file():
            sys.stderr.write("errors load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_errors_load(args.file))
    if args.group == "templates":
        if not args.file.is_file():
            sys.stderr.write("templates load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_templates_load(args.file))
    if args.group == "dictionary":
        return asyncio.run(run_dictionary_sync(args.file))
    if args.group == "jobs":
        if args.command == "requeue":
            return asyncio.run(run_requeue(args.job_id, args.staff_email, args.reason))
        return asyncio.run(run_dead_letter_list(args.tenant, args.limit))
    if args.group == "seed":
        return asyncio.run(run_seed(args.profile, reset=args.reset))
    return run_migrate(alembic_config())


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    raise SystemExit(main())
