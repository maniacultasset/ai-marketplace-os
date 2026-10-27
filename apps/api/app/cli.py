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

`templates load [--file PATH]` (S13 B7) loads a JSON list of `{version, product_type, template}`
into `category_templates` as `migrator`, in one transaction: a stored version with the same
content is a no-op, one with other content refuses the file ("bump version", exit 1, nothing
written), a new version is inserted and promoted when it is newer than the current one (audited
with a NULL tenant). An invalid file exits 1, a missing one 2. The default file is
`PLATFORM_TEMPLATES_PATH`, the Amazon and Flipkart mappings generated from `seed_ops`.

`rules load [--file PATH]` loads a JSON list of platform `RuleSet`s (default `RULES_SEED_PATH`)
into `rules` as `migrator`, in one transaction. A rule is matched on its fixed `id`: the same
content is a no-op, a difference in `enabled` alone flips it, any other difference refuses the
file ("bump version", exit 1, nothing written), and so does a new id on a stored
`rules_scope_key` or an id a tenant rule holds. A rule missing from the file is left as stored;
turn it off with `"enabled": false`. Each write is audited with a NULL tenant. An invalid file
exits 1, a missing one 2. Both loaders hold the migration advisory lock for their transaction.

The deploy's migrate job (`APP_ROLE=migrate`) runs `db migrate`, `dictionary sync`, `rules load`
and `templates load`, in that order and with no path: rules before templates, because generation
is gated on a current template only, so a failed rules load leaves no category generatable.

`errors load --file PATH` (S20 T4) loads a JSON list of `marketplace_error_map` rows as `migrator`,
in one transaction: a stored key with the same content is a no-op, one that differs only in
`enabled` flips it, any other difference refuses the file ("bump version", exit 1, nothing
written); each write is audited with a NULL tenant. An invalid file exits 1, a missing one 2.

`platform load [--error-map PATH]` (S20 T5b) loads every platform row a fresh database needs and
no tenant row: the S9 platform rows (templates, NULL-tenant rules, the dictionary), the Amazon
category mappings, and the error map (default: the local fake fixture). Like `seed` it runs only
under `APP_ENV=local` (exit 3); staging and production use the migrate job's `rules load` and
`templates load`, and `errors load` once observed error-map rows exist.

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

`tenant create --name N --slug S --owner-email E --staff-email F [--trial-days D]`,
`tenant add-member --slug S --email E --role R --staff-email F`,
`user set-password --email E --staff-email F` and `user make-admin --email E --staff-email F`
(ADR-0019: the platform admin who approves self-serve sign-ups; idempotent) are the staff
onboarding tools (S16 PLAN D6;
DATABASE.md:312): each is one migrator transaction on `ALEMBIC_DATABASE_URL` (the SQL is in
`app/db/staff_ops.py`), audited as `actor_type='staff'`. A new user's password comes only from
stdin (asked twice on a terminal), never from argv or the environment. Emails are trimmed,
lower-cased and must be what the contract's login email accepts (visible ASCII, one `@`).

DPP-1 (ADR-0021; runbook `docs/runbooks/MFA-STAFF.md`, identity check first): `user unlock --email
E --staff-email F` clears a locked account; `user reset-mfa --email E --staff-email F` deletes the
authenticator and recovery codes, sets a new must-change password from stdin and ends every
session. Every password staff set (a new user, `set-password`, `reset-mfa`) must meet the DPP
1.4.1 rules (exit 2 with the rule only) and is must-change; `set-password` and `reset-mfa` also
refuse any of the last 10.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| created / added / set / unlocked / reset | stdout | 0 |
| any other failure                    | stderr | 1    |
| bad arguments, or a password that breaks the rules | stderr | 2 |
| slug taken, unknown or stopped tenant or user, membership exists | stderr | 4 |
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
from typing import TYPE_CHECKING, Any, Final, TextIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.db.staff_ops import NewUser

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
_RULES_SEED: Final = "packages/schemas/fixtures/rules.seed.json"


def _rules_seed_path(here: Path) -> Path:
    """The repo's platform RuleSets file: the nearest parent that holds it (S26 T23: a copy of
    apps/api one level deeper, mutmut's `mutants/`, still finds it), else `parents[3]` as
    before."""
    for parent in here.parents:
        if (parent / _RULES_SEED).is_file():
            return parent / _RULES_SEED
    return here.parents[3] / _RULES_SEED


RULES_SEED_PATH: Final = _rules_seed_path(Path(__file__).resolve())
# The local fake error map (S20 D7): `FAKE-` codes only; seed and `platform load` read it. It ships
# in the image (app/db/seed_data), so the image's local-only `seed` role can load it too.
ERROR_MAP_FAKE_PATH: Final = Path(__file__).resolve().parent / "db/seed_data/error_map.fake.json"
# The platform templates the migrate job loads (`templates load` with no `--file`); generated from
# `seed_ops.platform_template_loads()`, and it ships in the image too.
PLATFORM_TEMPLATES_PATH: Final = (
    Path(__file__).resolve().parent / "db/seed_data/platform_templates.json"
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
            # S19 T22: acme's warning rule; rules are written by the admin login only
            admin = create_async_engine(admin_url)
            try:
                async with admin.begin() as connection:
                    await seed_ops.seed_warning_rule(connection)
            finally:
                await admin.dispose()
            await _seed_demo_listing()
    finally:
        await dispose_engine()
    # S20 C12, C18: the one platform-load path. The Amazon mappings are promoted after the demo
    # listing, so KURTA-001 stays pinned to S9's version 1 on fresh and existing databases.
    await _platform_load(admin_url, ERROR_MAP_FAKE_PATH)
    if profile == "demo":
        # S20 T21: the family pins the Amazon kurta mapping that `platform_load` just promoted.
        try:
            async with unit_of_work(seed_ops.seed_tenant_id("acme")) as session:
                await seed_ops.seed_demo_family(session)
            # S22 T15: acme's Flipkart listings, pinned to the Flipkart kurta mapping
            async with unit_of_work(seed_ops.seed_tenant_id("acme")) as session:
                await seed_ops.seed_demo_flipkart(session)
        finally:
            await dispose_engine()
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
            "APP_ENV=local; use rules load, templates load and errors load)\n"
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


async def _rules_load(admin_url: str, path: Path) -> tuple[int, int, int]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.platform_loads import load_rule_sets, load_rules

    rule_sets = load_rule_sets(path)
    engine = create_async_engine(admin_url)
    try:
        counts = await load_rules(engine, rule_sets)
    finally:
        await engine.dispose()
    return counts.inserted, counts.unchanged, counts.toggled


async def run_rules_load(path: Path) -> int:
    from pydantic import ValidationError

    from app.core.config import get_settings
    from app.db.platform_loads import RuleConflict

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write("rules load: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    try:
        inserted, unchanged, toggled = await _rules_load(settings.alembic_database_url, path)
    except RuleConflict as exc:
        sys.stderr.write(f"rules load: refused: {exc.key} (nothing was written)\n")
        return EXIT_FAILED
    except (ValidationError, ValueError) as exc:
        sys.stderr.write(f"rules load: invalid file ({type(exc).__name__}); nothing was written\n")
        return EXIT_FAILED
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"rules load: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    sys.stdout.write(
        f"rules load: ok ({inserted} inserted, {unchanged} unchanged, {toggled} toggled)\n"
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


# ---- staff onboarding (S16 T19-T20a) ----

EXIT_BAD_INPUT: Final = 2
SLUG: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,39}")
EMAIL_OCTETS_MAX: Final = 254
MEMBER_ROLES: Final = ("owner", "operator", "reviewer", "admin")


def _member_email(value: str) -> str:
    """Trimmed and lower-cased, then exactly what the contract's `LoginEmail` accepts (the
    `^[^@\\s]+@[^@\\s]+$` pattern plus visible ASCII, so a U+FEFF or other look-alike is refused)
    and what `ck_users_email` stores (at most 254 octets)."""
    from pydantic import TypeAdapter, ValidationError

    from app.api.v1.schemas.auth import LoginEmail

    email = value.strip().lower()
    try:
        TypeAdapter(LoginEmail).validate_python(email)
    except ValidationError:
        raise argparse.ArgumentTypeError("must be a plain ASCII email address") from None
    if len(email.encode("utf-8")) > EMAIL_OCTETS_MAX or email.count("@") != 1:
        raise argparse.ArgumentTypeError("must be a plain ASCII email address") from None
    return email


def _slug(value: str) -> str:
    if not SLUG.fullmatch(value):
        raise argparse.ArgumentTypeError("--slug must match ^[a-z0-9][a-z0-9-]{1,39}$")
    return value


def _name(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 200:
        raise argparse.ArgumentTypeError("--name must be 1-200 characters")
    return text


def _trial_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--trial-days must be an integer") from None
    if not 1 <= days <= 365:
        raise argparse.ArgumentTypeError("--trial-days must be in 1..365")
    return days


def _admin_engine() -> AsyncEngine:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    url = get_settings().alembic_database_url
    if url is None:
        raise RuntimeError("ALEMBIC_DATABASE_URL is required")
    return create_async_engine(url)


async def _new_user(engine: AsyncEngine, email: str, stdin: TextIO) -> NewUser:
    """The owner or member: an existing user (no password asked), or a new one whose password
    is read from stdin, checked against the DPP rules (`PolicyError`, exit 2) and hashed here."""
    from app.core import password_policy
    from app.core.passwords import hash_password, read_password
    from app.db import staff_ops

    if await staff_ops.user_exists(engine, email):
        return staff_ops.NewUser(email=email, password_hash=None)
    secret = read_password(stdin)
    breaches = password_policy.check(secret.get_secret_value(), email=email, display_name=None)
    if breaches:
        raise password_policy.PolicyError(breaches)
    return staff_ops.NewUser(email=email, password_hash=hash_password(secret.get_secret_value()))


async def run_tenant_command(args: argparse.Namespace, stdin: TextIO) -> int:
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db import staff_ops

    command = f"{args.group} {args.command}"
    try:
        engine = _admin_engine()
    except RuntimeError:
        sys.stderr.write(f"{command}: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    try:
        with request_scope(new_request_id()) as ctx:
            if args.group == "user" and args.command == "make-admin":
                user_id = await staff_ops.make_platform_admin(
                    engine,
                    email=args.email,
                    staff_email=args.staff_email,
                    request_id=ctx.request_id,
                )
                sys.stdout.write(f"user_id={user_id} platform_admin=true\n")
            elif args.group == "user" and args.command == "unlock":
                user_id = await staff_ops.unlock_user(
                    engine,
                    email=args.email,
                    staff_email=args.staff_email,
                    request_id=ctx.request_id,
                )
                sys.stdout.write(f"user_id={user_id} unlocked=true\n")
            elif args.group == "user" and args.command == "reset-mfa":
                from app.core.passwords import read_password

                secret = read_password(stdin)
                user_id, revoked = await staff_ops.reset_mfa(
                    engine,
                    email=args.email,
                    password=secret,
                    staff_email=args.staff_email,
                    request_id=ctx.request_id,
                )
                sys.stdout.write(f"user_id={user_id} mfa_reset=true revoked={revoked}\n")
            elif args.group == "user":
                from app.core.passwords import read_password

                secret = read_password(stdin)
                user_id, revoked = await staff_ops.set_password(
                    engine,
                    email=args.email,
                    password=secret,
                    staff_email=args.staff_email,
                    request_id=ctx.request_id,
                )
                sys.stdout.write(f"user_id={user_id} revoked={revoked}\n")
            elif args.command == "create":
                owner = await _new_user(engine, args.owner_email, stdin)
                tenant_id = await staff_ops.create_tenant_with_owner(
                    engine,
                    name=args.name,
                    slug=args.slug,
                    owner=owner,
                    staff_email=args.staff_email,
                    trial_days=args.trial_days,
                    request_id=ctx.request_id,
                )
                sys.stdout.write(f"tenant_id={tenant_id} slug={args.slug}\n")
            else:
                member = await _new_user(engine, args.email, stdin)
                tenant_id, user_id = await staff_ops.add_member(
                    engine,
                    slug=args.slug,
                    user=member,
                    role=args.role,
                    staff_email=args.staff_email,
                    request_id=ctx.request_id,
                )
                sys.stdout.write(f"tenant_id={tenant_id} user_id={user_id}\n")
    except ValueError as exc:  # the password reader and rules: fixed phrases, never the password
        sys.stderr.write(f"{command}: refused ({exc})\n")
        return EXIT_BAD_INPUT
    except staff_ops.SlugTakenError:
        sys.stderr.write(f"{command}: refused (the slug is taken)\n")
        return EXIT_NOT_ELIGIBLE
    except staff_ops.NotEligibleError as exc:
        sys.stderr.write(f"{command}: refused ({exc})\n")
        return EXIT_NOT_ELIGIBLE
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"{command}: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await engine.dispose()
    return EXIT_OK


def _add_onboarding(groups: Any) -> None:
    tenant = groups.add_parser("tenant", help="staff tenant onboarding (S16)")
    tenant_commands = tenant.add_subparsers(dest="command", required=True)
    create = tenant_commands.add_parser("create", help="a tenant and its first owner")
    create.add_argument("--name", type=_name, required=True)
    create.add_argument("--slug", type=_slug, required=True)
    create.add_argument("--owner-email", type=_member_email, required=True)
    create.add_argument("--staff-email", type=_member_email, required=True)
    create.add_argument("--trial-days", type=_trial_days, default=None)
    add = tenant_commands.add_parser("add-member", help="a member of a running tenant")
    add.add_argument("--slug", type=_slug, required=True)
    add.add_argument("--email", type=_member_email, required=True)
    add.add_argument("--role", choices=MEMBER_ROLES, required=True)
    add.add_argument("--staff-email", type=_member_email, required=True)
    user = groups.add_parser("user", help="staff user tools (S16)")
    user_commands = user.add_subparsers(dest="command", required=True)
    reset = user_commands.add_parser("set-password", help="a new password from stdin")
    reset.add_argument("--email", type=_member_email, required=True)
    reset.add_argument("--staff-email", type=_member_email, required=True)
    admin = user_commands.add_parser("make-admin", help="a platform admin (sign-up approvals)")
    admin.add_argument("--email", type=_member_email, required=True)
    admin.add_argument("--staff-email", type=_member_email, required=True)
    # DPP-1 (ADR-0021): after the runbook's identity check
    unlock = user_commands.add_parser("unlock", help="clear a locked account (DPP-1)")
    unlock.add_argument("--email", type=_member_email, required=True)
    unlock.add_argument("--staff-email", type=_member_email, required=True)
    reset_mfa = user_commands.add_parser(
        "reset-mfa", help="delete the authenticator; a new must-change password from stdin"
    )
    reset_mfa.add_argument("--email", type=_member_email, required=True)
    reset_mfa.add_argument("--staff-email", type=_member_email, required=True)


def build_parser() -> argparse.ArgumentParser:
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
    load.add_argument("--file", type=Path, default=PLATFORM_TEMPLATES_PATH)
    rules = groups.add_parser("rules", help="the platform rules (migrator)")
    rules_commands = rules.add_subparsers(dest="command", required=True)
    rules_load = rules_commands.add_parser("load", help="load versioned platform rules")
    rules_load.add_argument("--file", type=Path, default=RULES_SEED_PATH)
    errors = groups.add_parser("errors", help="the marketplace error map (migrator)")
    errors_commands = errors.add_subparsers(dest="command", required=True)
    errors_load = errors_commands.add_parser("load", help="load versioned error-map rows")
    errors_load.add_argument("--file", type=Path, required=True)
    platform = groups.add_parser("platform", help="every platform row, no tenant (local only)")
    platform_commands = platform.add_subparsers(dest="command", required=True)
    platform_load = platform_commands.add_parser("load", help="templates, rules, error map")
    platform_load.add_argument("--error-map", type=Path, default=ERROR_MAP_FAKE_PATH)
    _add_onboarding(groups)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.group in ("tenant", "user"):
        return asyncio.run(run_tenant_command(args, sys.stdin))
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
    if args.group == "rules":
        if not args.file.is_file():
            sys.stderr.write("rules load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_rules_load(args.file))
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
