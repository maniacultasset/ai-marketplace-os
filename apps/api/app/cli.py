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

The deploy's migrate job (`APP_ROLE=migrate`) runs `db migrate`, `dictionary sync`, `rules load`,
`templates load`, `errors load` and `errors load --marketplace flipkart`, in that order and with no
path: rules before templates, because
generation is gated on a current template only, so a failed rules load leaves no category
generatable.

`errors load [--file PATH] [--marketplace amazon|flipkart]` (S20 T4; SI9: default
`app/db/seed_data/error_map.amazon.json`, the Amazon rows taken from Amazon's published SP-API
material; `--marketplace flipkart` defaults to `error_map.flipkart.json`, the Flipkart rows keyed
on the documented payload node an issue's path names) loads a JSON list of `marketplace_error_map` rows as `migrator`,
in one transaction: a stored key with the same content is a no-op, one that differs only in
`enabled` flips it, any other difference refuses the file ("bump version", exit 1, nothing
written); each write is audited with a NULL tenant. An invalid file exits 1, a missing one 2.

`errors proposals list|approve|reject` (SI13 T14): the staff review of AI-drafted error-map rows
(`app.db.proposal_review`); approve prints the seed row for the PR.
`errors catalog [--unmapped] [--limit 1..500]` (SI11 T19) prints the marketplace errors seen, one
JSON line per (marketplace, code, error code, class): mapped or not, the lane, counts, tenants,
first and last seen, the resolutions and, for an unmapped code, a suggested map row to review in a
PR. It reads the `marketplace_error_catalog` view through `platform_scope()`; no tenant id or
message text is printed (the view holds neither).

`platform load [--error-map PATH]` (S20 T5b) loads every platform row a fresh database needs and
no tenant row: the S9 platform rows (templates, NULL-tenant rules, the dictionary), the Amazon
category mappings, and the error map (default: the local fake fixture). Like `seed` it runs only
under `APP_ENV=local` (exit 3); staging and production use the migrate job's `rules load` and
`templates load` and `errors load`.

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
`tenant activate --slug S --reason R --staff-email F` and `tenant extend-trial --slug S --days
1..90 --reason R --staff-email F` (SI5, ADR-0029: trial recovery; exit 4 for an unknown slug or a
refused move),
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

DW2 (T53): the Amazon Product Type Definitions tools. They run under the platform-owned SP-API
authorization (`AMAZON_PLATFORM_REFRESH_TOKEN`, read only here; never a tenant token) and touch no
tenant table. Every write is one migrator transaction on `ALEMBIC_DATABASE_URL` (the SQL is in
`app/db/ptd_loads.py`), audited as `actor_type='staff'` with a NULL tenant. Output names the
product type, version and digest prefix only: never the schema link, a token or the schema.

`templates refresh --product-type T [--requirements R] --staff-email E --reason R [--dry-run
[--save-schema PATH]]` fetches the definition and its schema (checksum verified), then stores it:
the same digest again is `unchanged`; a new one is `stored`, and current only when the key has no
current version yet (`stored_current`). `--dry-run` fetches only and needs no database;
`--save-schema` writes the verified schema to a new file (mode 0600, never over an existing file
or a link).

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| fetched / unchanged / stored / stored_current | stdout | 0 |
| a missing setting, any failure       | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |

`templates promote-definition --product-type T --version V [--digest PREFIX] [--requirements R]
--staff-email E --reason R` makes a stored version current (after a `templates diff`).

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| promoted, or already current         | stdout | 0    |
| any failure                          | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| no such version, or the version needs `--digest` | stderr | 4 |

`ops amazon-check (--tenant-slug S | --seller-id ID) --staff-email E [--ean EAN] [--asin ASIN]`
starts the read-only Amazon connection check (`app.workers.amazon_check`) for the tenant: it
resolves the slug, or the tenant whose connected (not revoked) Amazon account has that seller id
(read-only, `platform_scope`; it refuses unless exactly one tenant matches), prints the tenant's
slug (the seller id masked) and replaces this process with `python -m app.workers.amazon_check
--tenant-id ...` (`os.execv`). The check opens a tenant token, so it runs as a worker process: this
module never imports the decrypt path or the SP-API client. The worker prints the table and sets
the exit code (0 no step failed, 1 a step failed).

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| handed over to the worker check      | -      | the check's |
| any failure before the hand-over     | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| no such tenant, or a seller id that matches no tenant or more than one | stderr | 4 |

`ops amazon-preview --tenant-slug S --listing-id L --staff-email E [--precheck]` is the owner's
one-off Amazon dry run (2026-11-12; docs/runbooks/AMAZON-PREVIEW-ONEOFF.md): it resolves the slug
as `ops amazon-check` does and replaces this process with `python -m app.workers.amazon_preview
--tenant-id ... --listing-id ...` (`os.execv`). The worker entrypoint holds every guard (it
refuses unless `AMAZON_PREVIEW_ONEOFF` is set) and sets the exit code (0 done, 3 refused, 4 not
eligible, 5 STOP).

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| handed over to the worker            | -      | the worker's |
| any failure before the hand-over     | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| no such tenant                       | stderr | 4    |

`images recheck (--tenant ID | --tenant-slug S | --seller-id ID) [--staff-email E]` queues the
photo checks again for the tenant's live photos judged by the old photo rules, at most the free
per-brand job cap per run (`DATABASE_URL`, tenant scope). The slug and the seller id resolve as in
`ops amazon-check` (the seller id masked). It prints `queued=N remaining=M`; run it again until
`remaining=0`. With `--staff-email`, one `images.recheck` audit row names it.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| queued (any count, `remaining` may be above 0) | stdout | 0 |
| any failure                          | stderr | 1    |
| bad arguments (argparse)             | stderr | 2    |
| no such tenant, or a seller id that matches no tenant or more than one | stderr | 4 |

`templates check [--product-type T] [--requirements R] [--list-browse-nodes]` (client item 5)
checks every Amazon category template of `PLATFORM_TEMPLATES_PATH` against the stored current
definition of its own product type (read-only on `DATABASE_URL`): per template, the stored
version and its drift lines, or "no stored definition ..., not checked", and whether its browse
node is set. `--list-browse-nodes` also prints the nodes a definition lists.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| every template checked, no drift     | stdout | 0    |
| drift in any template                | stdout | 1    |
| bad arguments (argparse)             | stderr | 2    |
| a template not checked (no stored definition), no drift | stdout | 3 |
| could not compare (no database, no template, a failure) | stderr | 3 |

`templates diff --product-type T [--requirements R] [--from-file PATH]` reports the believed
Amazon wire names (the adapter's constants and `PLATFORM_TEMPLATES_PATH`) against the stored
current definition (read-only on `DATABASE_URL`) or a saved schema file. Value-free lines.

| outcome                              | stream | exit |
|--------------------------------------|--------|------|
| no drift                             | stdout | 0    |
| drift (one line per drift)           | stdout | 1    |
| `--from-file` missing, bad arguments | stderr | 2    |
| could not compare (no stored definition, unreadable file, a failure) | stderr | 3 |
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TextIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.adapters.amazon.definitions import DefinitionsClient, FetchedDefinition
    from app.core.config import Settings
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
ERROR_MAP_AMAZON_PATH: Final = (
    Path(__file__).resolve().parent / "db/seed_data/error_map.amazon.json"
)
ERROR_MAP_FLIPKART_PATH: Final = (
    Path(__file__).resolve().parent / "db/seed_data/error_map.flipkart.json"
)
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


def _catalog_limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--limit must be an integer") from None
    if not 1 <= number <= 500:
        raise argparse.ArgumentTypeError("--limit must be in 1..500")
    return number


async def run_errors_catalog(*, unmapped: bool, limit: int) -> int:
    """SI11 T19: the marketplace error catalogue as JSON lines (marketplace, code, mapped, error
    code, class, lane, counts, dates, resolutions, the suggested map row of an unmapped code).
    No tenant id and no message text: the view holds neither."""
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.error_catalog import catalog_rows
    from app.services.resolution.catalog import catalog_line

    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                rows = await catalog_rows(session, unmapped=unmapped, limit=limit)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"errors catalog: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    for row in rows:
        sys.stdout.write(json.dumps(catalog_line(row), sort_keys=True) + "\n")
    sys.stderr.write(f"errors catalog: {len(rows)} code(s)\n")
    return EXIT_OK


PHOTO_DAYS_MAX = 365


# ---- SI13 T15: value-map proposals from outcomes (read-only, staff) ----


async def run_value_maps(min_listings: int) -> int:
    """`errors value-maps`: JSON lines (no tenant, listing, SKU or message) and the ready alias
    lines; nothing is written."""
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine, platform_scope
    from app.services.resolution.value_map_report import value_map_report

    try:
        with request_scope(new_request_id()):
            async with platform_scope() as platform:
                found, unpaired = await value_map_report(platform, min_listings)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"errors value-maps: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    for p in found:
        line = {
            "marketplace": p.marketplace,
            "product_type": p.product_type,
            "key": p.key,
            "v": p.v,
            "w": p.w,
            "listings": p.listings,
            "tenants": p.tenants,
            "alias": p.alias_line(),
        }
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    for u in unpaired:
        line = {
            "key": u.key,
            "v": u.v,
            "unpaired_listings": u.listings,
            "marketplace": u.marketplace,
        }
        sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stderr.write(f"errors value-maps: {len(found)} proposal(s), {len(unpaired)} unpaired\n")
    return EXIT_OK


# ---- SI13 T16: the Auto / Seller / Marketplace export (read-only, staff) ----


async def run_errors_export(fmt: str, marketplace: str | None) -> int:
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.error_catalog import map_rows, unmapped_count
    from app.services.resolution.export import rows, to_csv, to_md

    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                unmapped = await unmapped_count(session, marketplace)
                table = rows(await map_rows(session, marketplace), unmapped)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"errors export: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    sys.stdout.write(to_csv(table, unmapped) if fmt == "csv" else to_md(table, unmapped))
    return EXIT_OK


# ---- SI13 T14: the staff review of AI-drafted error-map proposals ----


async def run_proposals_list(*, status: str | None, limit: int) -> int:
    """The proposals as JSON lines (no tenant id, no sample issue, agent run or job id)."""
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.error_proposals import list_rows

    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                rows = await list_rows(session, status=status, limit=limit)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"errors proposals: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    for row in rows:
        sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
    sys.stderr.write(f"errors proposals: {len(rows)} proposal(s)\n")
    return EXIT_OK


async def run_proposal_review(args: argparse.Namespace) -> int:
    """`errors proposals approve|reject` (module `app.db.proposal_review`), as the migrator."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings
    from app.db.proposal_review import ReviewRefused, approve, reject

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write("errors proposals: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    engine = create_async_engine(settings.alembic_database_url)
    try:
        async with engine.begin() as connection:
            if args.command_proposals == "reject":
                await reject(connection, args.proposal_id, by=args.by, note=args.note)
                sys.stdout.write(f"errors proposals: rejected {args.proposal_id}\n")
                return EXIT_OK
            overrides = {
                "plain_cause": args.plain_text,
                "suggested_fix": args.seller_action,
                "error_code": args.error_code,
                "error_class": args.error_class,
            }
            done = await approve(connection, args.proposal_id, by=args.by, overrides=overrides)
    except ReviewRefused as exc:
        sys.stderr.write(f"errors proposals: refused ({exc.reason}); nothing was written\n")
        return EXIT_BAD_INPUT
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"errors proposals: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await engine.dispose()
    if done.replay:
        sys.stdout.write("errors proposals: already approved\n")
        return EXIT_OK
    sys.stdout.write(json.dumps(done.seed, sort_keys=True, indent=2) + "\n")
    sys.stderr.write("errors proposals: approved; add the row above to error_map.amazon.json\n")
    return EXIT_OK


def _days(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--days must be an integer") from None
    if not 1 <= number <= PHOTO_DAYS_MAX:
        raise argparse.ArgumentTypeError(f"--days must be in 1..{PHOTO_DAYS_MAX}")
    return number


async def run_photo_accuracy(days: int, *, now: datetime | None = None) -> int:
    """SI10 T18: how sellers reviewed photo-read values, per key, over the last `days` days,
    every tenant summed. Prints keys, counts and rates only: no tenant, product or value."""
    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.photo_accuracy import fact_updates_since
    from app.services.photo_accuracy import summarise

    since = (now or datetime.now(UTC)) - timedelta(days=days)
    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                rows = await fact_updates_since(session, since)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"ops photo-accuracy: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    found = summarise(rows)
    sys.stdout.write(
        f"{'key':<32} {'confirmed':>9} {'rejected':>8} {'changed':>7} {'accept':>6} {'conf':>5}\n"
    )
    for row in found:
        mean = "-" if row.mean_confidence is None else str(row.mean_confidence)
        sys.stdout.write(
            f"{row.key[:32]:<32} {row.confirmed:>9} {row.rejected:>8} {row.changed:>7} "
            f"{row.accept_rate!s:>6} {mean:>5}\n"
        )
    sys.stdout.write(f"ops photo-accuracy: {len(found)} key(s) over {days} day(s)\n")
    return EXIT_OK


_EAN_RE: Final = re.compile(r"[0-9]{8,14}", re.ASCII)
_ASIN_RE: Final = re.compile(r"[A-Z0-9]{10}", re.ASCII)
_SELLER_RE: Final = re.compile(r"[A-Z0-9]{6,32}", re.ASCII)
AMAZON_CHECK_MODULE: Final = "app.workers.amazon_check"
AMAZON_PREVIEW_MODULE: Final = "app.workers.amazon_preview"


def _ean(value: str) -> str:
    if not _EAN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--ean must be 8 to 14 digits")
    return value


def _asin(value: str) -> str:
    if not _ASIN_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--asin must be 10 of [A-Z0-9]")
    return value


def _seller_id(value: str) -> str:
    # the message never echoes the value: a seller id is not printed whole
    if not _SELLER_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--seller-id must be 6 to 32 of [A-Z0-9]")
    return value


async def _tenants_by_seller(seller_id: str) -> list[tuple[uuid.UUID, str]]:
    """Every tenant (id, slug) with a connected, not revoked Amazon account of this seller id."""
    from sqlalchemy import select

    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.models.catalog_assets import MarketplaceAccount
    from app.db.models.tenancy import Tenant
    from app.db.session import dispose_engine, platform_scope

    query = (
        select(Tenant.id, Tenant.slug)
        .join(MarketplaceAccount, MarketplaceAccount.tenant_id == Tenant.id)
        .where(
            MarketplaceAccount.marketplace == "amazon",
            MarketplaceAccount.seller_id == seller_id,
            MarketplaceAccount.status != "revoked",
        )
        .distinct()
        .order_by(Tenant.slug)
    )
    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                found = await session.execute(query)
                return [(row[0], row[1]) for row in found.all()]
    finally:
        await dispose_engine()


async def _tenant_by_slug(slug: str) -> uuid.UUID | None:
    from sqlalchemy import select

    from app.core.request_context import new_request_id
    from app.core.request_context import request_context as request_scope
    from app.db.models.tenancy import Tenant
    from app.db.session import dispose_engine, platform_scope

    try:
        with request_scope(new_request_id()):
            async with platform_scope() as session:
                found = await session.execute(select(Tenant.id).where(Tenant.slug == slug))
                tenant_id: uuid.UUID | None = found.scalar_one_or_none()
                return tenant_id
    finally:
        await dispose_engine()


def amazon_check_argv(args: argparse.Namespace, tenant_id: uuid.UUID) -> list[str]:
    """The worker entrypoint's command line (flags only from the validated arguments)."""
    argv = [sys.executable, "-m", AMAZON_CHECK_MODULE, "--tenant-id", str(tenant_id)]
    argv += ["--staff-email", args.staff_email]
    if args.ean is not None:
        argv += ["--ean", args.ean]
    if args.asin is not None:
        argv += ["--asin", args.asin]
    return argv


# Tests swap this for a recorder; the real one never returns.
EXEC: Callable[[str, list[str]], Any] = os.execv


def resolve_tenant(label: str, tenant_slug: str | None, seller_id: str | None) -> uuid.UUID | int:
    """The tenant of a staff command, by slug or by the seller id of its connected Amazon
    account (exactly one tenant must match), printed as `LABEL: tenant SLUG (seller MASKED), id
    ID`; or the exit code after a stderr line (the seller id masked, never whole)."""
    from app.core.redaction import mask_seller

    try:
        if seller_id is not None:
            matches = asyncio.run(_tenants_by_seller(seller_id))
        elif tenant_slug is not None:
            found = asyncio.run(_tenant_by_slug(tenant_slug))
            matches = [] if found is None else [(found, tenant_slug)]
        else:  # argparse requires one of the two
            matches = []
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"{label}: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    if seller_id is not None and len(matches) != 1:
        seller = mask_seller(seller_id)
        if not matches:
            sys.stderr.write(f"{label}: no tenant has seller {seller} connected\n")
        else:
            sys.stderr.write(
                f"{label}: {len(matches)} tenants have seller {seller} connected; "
                "pass --tenant-slug instead\n"
            )
        return EXIT_NOT_ELIGIBLE
    if not matches:
        sys.stderr.write(f"{label}: no such tenant\n")
        return EXIT_NOT_ELIGIBLE
    tenant_id, slug = matches[0]
    via = "" if seller_id is None else f" (seller {mask_seller(seller_id)})"
    sys.stdout.write(f"{label}: tenant {slug}{via}, id {tenant_id}\n")
    return tenant_id


def run_amazon_check(args: argparse.Namespace) -> int:
    """Resolve the tenant (by slug or by seller id), then hand the process over to the worker's
    connection check."""
    tenant_id = resolve_tenant("amazon check", args.tenant_slug, args.seller_id)
    if isinstance(tenant_id, int):
        return tenant_id
    argv = amazon_check_argv(args, tenant_id)
    sys.stdout.flush()
    sys.stderr.flush()
    EXEC(argv[0], argv)
    return EXIT_OK


def amazon_preview_argv(args: argparse.Namespace, tenant_id: uuid.UUID) -> list[str]:
    """The one-off dry run's command line (flags only from the validated arguments)."""
    argv = [sys.executable, "-m", AMAZON_PREVIEW_MODULE, "--tenant-id", str(tenant_id)]
    argv += ["--listing-id", str(args.listing_id), "--staff-email", args.staff_email]
    if args.precheck:
        argv.append("--precheck")
    return argv


def run_amazon_preview(args: argparse.Namespace) -> int:
    """Resolve the tenant by slug, then hand the process over to the worker's one-off dry run."""
    tenant_id = resolve_tenant("amazon preview", args.tenant_slug, None)
    if isinstance(tenant_id, int):
        return tenant_id
    argv = amazon_preview_argv(args, tenant_id)
    sys.stdout.flush()
    sys.stderr.flush()
    EXEC(argv[0], argv)
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


# ---- SI1 T22: `images unpublish --tenant` (CP9) and `images derive --tenant` (CP6) ----


def _tenant_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--tenant must be a tenant UUID") from None


async def run_images_unpublish(tenant_id: uuid.UUID) -> int:
    """Delete every public copy of one tenant and NULL the names, as the migrator: the step
    before a by-hand `purge_tenant`, which the 0015 delete guard otherwise refuses."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.adapters.storage import get_storage
    from app.core.config import get_settings
    from app.db.image_purge import public_names_left, purge_public_images

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write("images unpublish: failed (ALEMBIC_DATABASE_URL is required)\n")
        return EXIT_FAILED
    engine = create_async_engine(settings.alembic_database_url)
    try:
        async with engine.begin() as connection:
            done = await purge_public_images(connection, get_storage(), tenant_id)
            left = await public_names_left(connection, tenant_id)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"images unpublish: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await engine.dispose()
    sys.stdout.write(f"images unpublish: ok (unpublished={done} remaining={left})\n")
    return EXIT_OK if left == 0 else EXIT_FAILED


async def run_images_derive(tenant_id: uuid.UUID) -> int:
    """Queue `image_compliance` for the tenant's live images that have no clean copy, at most
    the free live-job cap per run (CP6); prints how many are queued and left."""
    from mkos_schemas.enums import JobKind

    from app.db.session import dispose_engine
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from app.services.images import LIVE_IMAGE_JOB_CAP

    try:
        async with tenant_uow(tenant_id) as uow:
            await uow.images.lock_tenant_image_jobs()
            ids = await uow.images.live_without_derivatives()
            free = max(0, LIVE_IMAGE_JOB_CAP - await uow.images.count_live_image_jobs())
            for image_id in ids[:free]:
                await uow.job_runs.enqueue(
                    EnqueueSpec(
                        kind=JobKind.IMAGE_COMPLIANCE,
                        entity_type="image",
                        entity_id=image_id,
                        created_by=None,
                        payload={"image_id": str(image_id)},
                    )
                )
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"images derive: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    queued = min(free, len(ids))
    sys.stdout.write(f"images derive: ok (queued={queued} remaining={len(ids) - queued})\n")
    return EXIT_OK


async def run_images_recheck(tenant_id: uuid.UUID, staff_email: str | None = None) -> int:
    """Photo rules 2.1.0: queue `image_compliance` again for the tenant's live images whose
    stored report was judged by the old rules (an issue without a severity), at most the free
    live-job cap per run; prints how many are queued and left. Run again until `remaining=0`.
    The job writes the new report only; a listing's photo issues follow on its next check
    (a read of a reviewable listing, "Check again", or a regenerate). Nothing is approved or
    sent. With `staff_email`, the same transaction writes one `images.recheck` audit row
    naming it, its reason holding the counts."""
    from mkos_schemas.enums import JobKind

    from app.db.session import dispose_engine
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from app.services.images import LIVE_IMAGE_JOB_CAP

    try:
        async with tenant_uow(tenant_id) as uow:
            await uow.images.lock_tenant_image_jobs()
            ids = await uow.images.live_judged_by_old_rules()
            free = max(0, LIVE_IMAGE_JOB_CAP - await uow.images.count_live_image_jobs())
            for image_id in ids[:free]:
                await uow.job_runs.enqueue(
                    EnqueueSpec(
                        kind=JobKind.IMAGE_COMPLIANCE,
                        entity_type="image",
                        entity_id=image_id,
                        created_by=None,
                        payload={"image_id": str(image_id)},
                    )
                )
            if staff_email is not None:
                queued = min(free, len(ids))
                await uow.audit.write(
                    tenant_id=tenant_id,
                    actor_type="staff",
                    actor_user_id=None,
                    actor_email=staff_email,
                    action="images.recheck",
                    entity_type="tenant",
                    entity_id=tenant_id,
                    # the tenant allow-list keeps no counts in `after`: they go in the reason
                    reason=f"photo_rules_recheck queued={queued} remaining={len(ids) - queued}",
                )
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"images recheck: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await dispose_engine()
    queued = min(free, len(ids))
    sys.stdout.write(f"images recheck: ok (queued={queued} remaining={len(ids) - queued})\n")
    return EXIT_OK


def run_images_recheck_cli(args: argparse.Namespace) -> int:
    """`images recheck`: the tenant by id, or resolved by slug or seller id (as `ops
    amazon-check` does, the seller id masked), then one recheck run."""
    if args.tenant is not None:
        return asyncio.run(run_images_recheck(args.tenant, args.staff_email))
    resolved = resolve_tenant("images recheck", args.tenant_slug, args.seller_id)
    if isinstance(resolved, int):
        return resolved
    return asyncio.run(run_images_recheck(resolved, args.staff_email))


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


def _extend_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--days must be an integer") from None
    if not 1 <= days <= 90:
        raise argparse.ArgumentTypeError("--days must be in 1..90")
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
            elif args.command == "activate":
                tenant_id, outcome = await staff_ops.activate_tenant(
                    engine, slug=args.slug, reason=args.reason, staff_email=args.staff_email
                )
                sys.stdout.write(f"tenant_id={tenant_id} outcome={outcome}\n")
            elif args.command == "extend-trial":
                tenant_id, outcome = await staff_ops.extend_trial(
                    engine,
                    slug=args.slug,
                    days=args.days,
                    reason=args.reason,
                    staff_email=args.staff_email,
                )
                sys.stdout.write(f"tenant_id={tenant_id} outcome={outcome}\n")
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


# ---- DW2: Amazon Product Type Definitions ----

EXIT_DRIFT: Final = 1
EXIT_NO_COMPARE: Final = 3
_PRODUCT_TYPE_RE: Final = re.compile(r"[A-Z0-9_]{1,64}")
_VERSION_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
_DIGEST_PREFIX_RE: Final = re.compile(r"[0-9a-fA-F]{4,64}")
PTD_SETTINGS: Final = (
    ("AMAZON_CLIENT_ID", "amazon_client_id"),
    ("AMAZON_CLIENT_SECRET", "amazon_client_secret"),
    ("AMAZON_PLATFORM_REFRESH_TOKEN", "amazon_platform_refresh_token"),
)


def _product_type(value: str) -> str:
    if not _PRODUCT_TYPE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--product-type must match [A-Z0-9_]{1,64}")
    return value


def _ptd_version(value: str) -> str:
    if not _VERSION_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--version must be 1-128 of [A-Za-z0-9._-]")
    return value


def _digest_prefix(value: str) -> str:
    if not _DIGEST_PREFIX_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("--digest must be 4-64 hex characters")
    return value.lower()


def _missing_ptd_setting(settings: Settings) -> str | None:
    for name, attribute in PTD_SETTINGS:
        value = getattr(settings, attribute)
        text = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        if text is None or not str(text).strip():
            return name
    return None


def build_definitions_client(settings: Settings) -> DefinitionsClient:
    """The platform PTD client: LWA refresh under the platform token, an in-process limiter (C1),
    an SP-API client with no seller, and the link policy from `AMAZON_PTD_LINK_HOSTS`."""
    from app.adapters.amazon import constants as amazon
    from app.adapters.amazon.client import SpApiClient
    from app.adapters.amazon.definitions import (
        DefinitionsClient,
        IntervalLimiter,
        LinkPolicy,
        PlatformTokenSource,
    )
    from app.adapters.oauth.amazon import AmazonOAuthClient

    refresh = settings.amazon_platform_refresh_token
    if (
        refresh is None
        or settings.amazon_client_id is None
        or settings.amazon_client_secret is None
    ):
        raise ValueError("the platform authorization is not configured")
    oauth = AmazonOAuthClient(
        application_id=settings.amazon_app_id or "",
        client_id=settings.amazon_client_id,
        client_secret=settings.amazon_client_secret,
        authorize_base=settings.amazon_authorize_url or "",
        token_url=settings.amazon_lwa_token_url,
        redirect_uri=settings.amazon_oauth_redirect_uri,
    )
    sp = SpApiClient(
        endpoint=settings.amazon_spapi_endpoint,
        marketplace_id=settings.amazon_marketplace_id,
        seller_id=None,
        tokens=PlatformTokenSource(oauth, refresh),
        limiter=IntervalLimiter(amazon.PTD_RATES),
    )
    return DefinitionsClient(
        sp,
        marketplace_id=settings.amazon_marketplace_id,
        link_policy=LinkPolicy.from_setting(settings.amazon_ptd_link_hosts),
    )


# Tests swap this factory for one over the in-process fake (no network).
DEFINITIONS_CLIENT: Callable[[Settings], DefinitionsClient] = build_definitions_client


def _save_schema(path: Path, schema: Any) -> None:
    """A new file, mode 0600; never over an existing file or through a link (L7)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(schema, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def _definition_line(outcome: str, fetched: FetchedDefinition) -> str:
    properties = fetched.schema.get("properties")
    required = fetched.schema.get("required")
    count = len(properties) if isinstance(properties, dict) else 0
    needed = len(required) if isinstance(required, list) else 0
    return (
        f"templates refresh: {outcome} {fetched.product_type} version "
        f"{fetched.product_type_version} digest {fetched.digest.hex()[:12]} "
        f"properties {count} required {needed}\n"
    )


async def _fetch_definition(
    settings: Settings, product_type: str, requirements: str
) -> FetchedDefinition:
    from app.adapters.amazon.endpoint import spapi_endpoint_errors

    problems = spapi_endpoint_errors(
        settings.amazon_spapi_endpoint,
        use_sandbox=settings.amazon_use_sandbox,
        app_env=settings.app_env,
    )
    if problems:
        raise ValueError("AMAZON_SPAPI_ENDPOINT does not match AMAZON_USE_SANDBOX")
    client = DEFINITIONS_CLIENT(settings)
    try:
        return await client.get_product_type(product_type, requirements=requirements)
    finally:
        await client.aclose()


async def run_ptd_refresh(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings
    from app.core.request_context import new_request_id
    from app.db.ptd_loads import Actor, DefinitionRow, store_definition

    settings = get_settings()
    missing = _missing_ptd_setting(settings)
    if missing is None and not args.dry_run and settings.alembic_database_url is None:
        missing = "ALEMBIC_DATABASE_URL"
    if missing is not None:
        sys.stderr.write(f"templates refresh: failed ({missing} is required)\n")
        return EXIT_FAILED
    try:
        fetched = await _fetch_definition(settings, args.product_type, args.requirements)
    except Exception as exc:  # class and code only, never the message (it may name the link)
        report = getattr(exc, "report", None)
        code = f", {report.error_code}" if report is not None and report.error_code else ""
        sys.stderr.write(f"templates refresh: failed ({type(exc).__name__}{code})\n")
        return EXIT_FAILED
    if args.dry_run:
        if args.save_schema is not None:
            try:
                _save_schema(args.save_schema, fetched.schema)
            except OSError as exc:
                sys.stderr.write(f"templates refresh: not saved ({type(exc).__name__})\n")
                return EXIT_FAILED
        sys.stdout.write(_definition_line("fetched", fetched))
        return EXIT_OK
    row = DefinitionRow(
        marketplace_id=fetched.marketplace_id,
        product_type=fetched.product_type,
        requirements=fetched.requirements,
        locale=fetched.locale,
        product_type_version=fetched.product_type_version,
        requirements_enforced=fetched.requirements_enforced,
        display_name=fetched.display_name,
        property_groups=fetched.property_groups,
        meta_schema_id=fetched.meta_schema_id,
        schema=fetched.schema,
        digest=fetched.digest,
    )
    actor = Actor(args.staff_email, args.reason, new_request_id())
    engine = create_async_engine(str(settings.alembic_database_url))
    try:
        result = await store_definition(engine, row, actor)
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"templates refresh: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await engine.dispose()
    sys.stdout.write(_definition_line(result.outcome, fetched))
    return EXIT_OK


async def run_ptd_promote(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings
    from app.core.request_context import new_request_id
    from app.db.ptd_loads import Actor, promote_definition

    settings = get_settings()
    if settings.alembic_database_url is None:
        sys.stderr.write(
            "templates promote-definition: failed (ALEMBIC_DATABASE_URL is required)\n"
        )
        return EXIT_FAILED
    from app.adapters.amazon import constants as amazon

    key = {
        "marketplace_id": settings.amazon_marketplace_id,
        "product_type": args.product_type,
        "requirements": args.requirements,
        "locale": amazon.PTD_LOCALE,
    }
    engine = create_async_engine(settings.alembic_database_url)
    try:
        promoted = await promote_definition(
            engine,
            key=key,
            version=args.version,
            digest_prefix=args.digest,
            actor=Actor(args.staff_email, args.reason, new_request_id()),
        )
    except LookupError:
        sys.stderr.write(
            "templates promote-definition: not found (no such version, or it needs --digest)\n"
        )
        return EXIT_NOT_ELIGIBLE
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"templates promote-definition: failed ({_describe(exc)})\n")
        return EXIT_FAILED
    finally:
        await engine.dispose()
    state = "promoted" if promoted else "already current"
    sys.stdout.write(
        f"templates promote-definition: {state} {args.product_type} version {args.version}\n"
    )
    return EXIT_OK


async def _stored_schema(settings: Settings, product_type: str, requirements: str) -> Any:
    from app.adapters.amazon import constants as amazon
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.product_type_definitions import ProductTypeDefinitionRepository

    try:
        async with platform_scope() as session:
            repo = ProductTypeDefinitionRepository(session)
            current = await repo.current(
                marketplace_id=settings.amazon_marketplace_id,
                product_type=product_type,
                requirements=requirements,
                locale=amazon.PTD_LOCALE,
            )
            if current is None:
                return None
            return await repo.schema_json(current.definition_id)
    finally:
        await dispose_engine()


def _amazon_templates(product_type: str) -> list[Any]:
    return [template for _, template in _amazon_category_templates(product_type)]


async def run_ptd_diff(args: argparse.Namespace) -> int:
    from app.adapters.amazon.definitions import SchemaInvalid, parse_schema
    from app.adapters.amazon.drift import diff, render
    from app.core.config import get_settings

    schema: Any
    try:
        if args.from_file is not None:
            schema = parse_schema(args.from_file.read_bytes())
        else:
            settings = get_settings()
            if settings.database_url is None:
                sys.stderr.write("templates diff: could not compare (DATABASE_URL is required)\n")
                return EXIT_NO_COMPARE
            schema = await _stored_schema(settings, args.product_type, args.requirements)
        templates = _amazon_templates(args.product_type)
    except SchemaInvalid:
        sys.stderr.write("templates diff: could not compare (the file is not a JSON schema)\n")
        return EXIT_NO_COMPARE
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"templates diff: could not compare ({_describe(exc)})\n")
        return EXIT_NO_COMPARE
    if schema is None:
        sys.stderr.write(
            f"templates diff: could not compare (no stored definition for {args.product_type})\n"
        )
        return EXIT_NO_COMPARE
    drifts = diff(schema, templates, args.product_type)
    if drifts:
        sys.stdout.write(render(drifts) + "\n")
    sys.stdout.write(f"templates diff: {len(drifts)} drift(s) for {args.product_type}\n")
    return EXIT_DRIFT if drifts else EXIT_OK


def _amazon_category_templates(product_type: str | None) -> list[tuple[str, Any]]:
    """The latest Amazon template of each category in `PLATFORM_TEMPLATES_PATH`, as
    (product_type, template); only `product_type`'s when given."""
    from app.db.platform_loads import load_template_file

    latest: dict[str, Any] = {}
    for entry in load_template_file(PLATFORM_TEMPLATES_PATH):
        template = entry.template
        if template.marketplace.value != "amazon" or not entry.product_type:
            continue
        if product_type is not None and entry.product_type != product_type:
            continue
        held = latest.get(template.category)
        if held is None or entry.version > held.version:
            latest[template.category] = entry
    return [(latest[k].product_type, latest[k].template) for k in sorted(latest)]


async def _stored_definitions(
    settings: Settings, product_types: Sequence[str], requirements: str
) -> dict[str, Any]:
    """product type -> its current stored definition (`StoredDefinition`), None when none."""
    from app.adapters.amazon import constants as amazon
    from app.adapters.amazon.drift import StoredDefinition
    from app.db.session import dispose_engine, platform_scope
    from app.repositories.product_type_definitions import ProductTypeDefinitionRepository

    out: dict[str, Any] = {}
    try:
        async with platform_scope() as session:
            repo = ProductTypeDefinitionRepository(session)
            for product_type in sorted(set(product_types)):
                current = await repo.current(
                    marketplace_id=settings.amazon_marketplace_id,
                    product_type=product_type,
                    requirements=requirements,
                    locale=amazon.PTD_LOCALE,
                )
                out[product_type] = (
                    None
                    if current is None
                    else StoredDefinition(
                        current.product_type_version,
                        await repo.schema_json(current.definition_id),
                    )
                )
    finally:
        await dispose_engine()
    return out


async def run_templates_check(args: argparse.Namespace) -> int:
    """Client item 5: every Amazon category template against the stored definition of its own
    product type; a template whose product type has none stored is reported as not checked."""
    from app.adapters.amazon.drift import check_templates, render_checks
    from app.core.config import get_settings

    try:
        templates = _amazon_category_templates(args.product_type)
        if not templates:
            sys.stderr.write("templates check: could not compare (no Amazon category template)\n")
            return EXIT_NO_COMPARE
        settings = get_settings()
        if settings.database_url is None:
            sys.stderr.write("templates check: could not compare (DATABASE_URL is required)\n")
            return EXIT_NO_COMPARE
        stored = await _stored_definitions(
            settings, [product_type for product_type, _ in templates], args.requirements
        )
    except Exception as exc:  # class and SQLSTATE only, never the message
        sys.stderr.write(f"templates check: could not compare ({_describe(exc)})\n")
        return EXIT_NO_COMPARE
    checks = check_templates(templates, stored)
    sys.stdout.write(render_checks(checks, list_nodes=args.list_browse_nodes) + "\n")
    if any(one.drifts for one in checks):
        return EXIT_DRIFT
    if not all(one.checked for one in checks):
        return EXIT_NO_COMPARE
    return EXIT_OK


def _add_ptd(templates_commands: Any) -> None:
    from app.adapters.amazon import constants as amazon

    def requirements(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--requirements",
            choices=amazon.PTD_REQUIREMENTS_VALUES,
            default=amazon.PTD_REQUIREMENTS,
        )

    refresh = templates_commands.add_parser(
        "refresh", help="fetch and store an Amazon product type definition (DW2)"
    )
    refresh.add_argument("--product-type", type=_product_type, required=True)
    requirements(refresh)
    refresh.add_argument("--staff-email", type=_staff_email, required=True)
    refresh.add_argument("--reason", type=_reason, required=True)
    refresh.add_argument("--dry-run", action="store_true", help="fetch only; no database")
    refresh.add_argument("--save-schema", type=Path, default=None, help="with --dry-run only")
    promote = templates_commands.add_parser(
        "promote-definition", help="make a stored definition version current (DW2)"
    )
    promote.add_argument("--product-type", type=_product_type, required=True)
    promote.add_argument("--version", type=_ptd_version, required=True)
    promote.add_argument("--digest", type=_digest_prefix, default=None)
    requirements(promote)
    promote.add_argument("--staff-email", type=_staff_email, required=True)
    promote.add_argument("--reason", type=_reason, required=True)
    drift = templates_commands.add_parser(
        "diff", help="believed Amazon names against a stored definition (DW2)"
    )
    drift.add_argument("--product-type", type=_product_type, required=True)
    requirements(drift)
    drift.add_argument("--from-file", type=Path, default=None)
    check = templates_commands.add_parser(
        "check", help="every Amazon category template against its stored definition"
    )
    check.add_argument("--product-type", type=_product_type, default=None)
    requirements(check)
    check.add_argument(
        "--list-browse-nodes", action="store_true", help="print the nodes each definition lists"
    )


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
    # SI5 (ADR-0029): trial recovery, the same 0017 functions a platform admin calls over HTTP
    activate = tenant_commands.add_parser("activate", help="a trial or suspended tenant -> active")
    activate.add_argument("--slug", type=_slug, required=True)
    activate.add_argument("--reason", type=_reason, required=True)
    activate.add_argument("--staff-email", type=_member_email, required=True)
    extend = tenant_commands.add_parser(
        "extend-trial", help="a trial ends later, or a trial-expired tenant reopens"
    )
    extend.add_argument("--slug", type=_slug, required=True)
    extend.add_argument("--days", type=_extend_days, required=True)
    extend.add_argument("--reason", type=_reason, required=True)
    extend.add_argument("--staff-email", type=_member_email, required=True)
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
    _add_ptd(templates_commands)
    rules = groups.add_parser("rules", help="the platform rules (migrator)")
    rules_commands = rules.add_subparsers(dest="command", required=True)
    rules_load = rules_commands.add_parser("load", help="load versioned platform rules")
    rules_load.add_argument("--file", type=Path, default=RULES_SEED_PATH)
    errors = groups.add_parser("errors", help="the marketplace error map (migrator)")
    errors_commands = errors.add_subparsers(dest="command", required=True)
    errors_load = errors_commands.add_parser("load", help="load versioned error-map rows")
    errors_load.add_argument("--file", type=Path, default=None)
    errors_load.add_argument("--marketplace", choices=("amazon", "flipkart"), default="amazon")
    errors_catalog = errors_commands.add_parser(
        "catalog", help="the marketplace errors seen, as JSON lines (staff, read-only)"
    )
    errors_catalog.add_argument("--unmapped", action="store_true")
    errors_catalog.add_argument("--limit", type=_catalog_limit, default=200)
    export = errors_commands.add_parser(
        "export", help="the Auto / Seller / Marketplace table from the error map (staff)"
    )
    export.add_argument("--format", choices=("md", "csv"), default="md")
    export.add_argument("--marketplace", choices=("amazon", "flipkart"))
    value_maps = errors_commands.add_parser(
        "value-maps", help="alias proposals from refused-then-live values (staff, read-only)"
    )
    value_maps.add_argument("--min", type=int, default=3, dest="min_listings")
    proposals = errors_commands.add_parser(
        "proposals", help="AI-drafted error-map rows: list, approve or reject (staff)"
    )
    proposal_commands = proposals.add_subparsers(dest="command_proposals", required=True)
    plist = proposal_commands.add_parser("list", help="the proposals as JSON lines")
    plist.add_argument(
        "--status", choices=("pending", "proposed", "approved", "rejected", "invalid", "failed")
    )
    plist.add_argument("--limit", type=_catalog_limit, default=200)
    papprove = proposal_commands.add_parser("approve", help="write the map row (migrator)")
    papprove.add_argument("proposal_id", type=uuid.UUID)
    papprove.add_argument("--by", required=True, help="an active platform admin's email")
    papprove.add_argument("--plain-text")
    papprove.add_argument("--seller-action")
    papprove.add_argument("--error-code")
    papprove.add_argument("--error-class")
    preject = proposal_commands.add_parser("reject", help="close the proposal (migrator)")
    preject.add_argument("proposal_id", type=uuid.UUID)
    preject.add_argument("--by", required=True)
    preject.add_argument("--note", required=True)
    images = groups.add_parser("images", help="image copies (SI1, staff)")
    images_commands = images.add_subparsers(dest="command", required=True)
    unpublish = images_commands.add_parser(
        "unpublish", help="delete a tenant's public image copies (before a by-hand purge)"
    )
    unpublish.add_argument("--tenant", type=_tenant_uuid, required=True)
    derive_cmd = images_commands.add_parser(
        "derive", help="queue the checks again for images with no clean copy"
    )
    derive_cmd.add_argument("--tenant", type=_tenant_uuid, required=True)
    recheck_cmd = images_commands.add_parser(
        "recheck", help="queue the checks again for images judged by the old photo rules"
    )
    recheck_for = recheck_cmd.add_mutually_exclusive_group(required=True)
    recheck_for.add_argument("--tenant", type=_tenant_uuid, default=None)
    recheck_for.add_argument("--tenant-slug", type=_slug, default=None)
    recheck_for.add_argument(
        "--seller-id",
        type=_seller_id,
        default=None,
        help="find the tenant by its connected Amazon account's seller id",
    )
    recheck_cmd.add_argument(
        "--staff-email", type=_staff_email, default=None, help="named in one audit row"
    )
    platform = groups.add_parser("platform", help="every platform row, no tenant (local only)")
    platform_commands = platform.add_subparsers(dest="command", required=True)
    platform_load = platform_commands.add_parser("load", help="templates, rules, error map")
    platform_load.add_argument("--error-map", type=Path, default=ERROR_MAP_FAKE_PATH)
    ops = groups.add_parser("ops", help="staff read-only reports (SI10)")
    ops_commands = ops.add_subparsers(dest="command", required=True)
    accuracy = ops_commands.add_parser(
        "photo-accuracy", help="how sellers confirmed, rejected or changed photo-read values"
    )
    accuracy.add_argument("--days", type=_days, default=30)
    check = ops_commands.add_parser(
        "amazon-check", help="the read-only Amazon connection check of one tenant (GET only)"
    )
    tenant = check.add_mutually_exclusive_group(required=True)
    tenant.add_argument("--tenant-slug", type=_slug, default=None)
    tenant.add_argument(
        "--seller-id",
        type=_seller_id,
        default=None,
        help="find the tenant by its connected Amazon account's seller id",
    )
    check.add_argument("--staff-email", type=_staff_email, required=True)
    check.add_argument("--ean", type=_ean, default=None, help="the EAN to search the catalog by")
    check.add_argument("--asin", type=_asin, default=None, help="the ASIN to read restrictions of")
    preview = ops_commands.add_parser(
        "amazon-preview",
        help="the owner's one-off Amazon dry run of one listing (VALIDATION_PREVIEW, saves nothing)",
    )
    preview.add_argument("--tenant-slug", type=_slug, required=True)
    preview.add_argument("--listing-id", type=uuid.UUID, required=True)
    preview.add_argument("--staff-email", type=_staff_email, required=True)
    preview.add_argument(
        "--precheck", action="store_true", help="also the catalog and restriction pre-check"
    )
    _add_onboarding(groups)
    return parser


def _run_ptd(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "refresh":
        if args.save_schema is not None and not args.dry_run:
            parser.error("--save-schema needs --dry-run")
        return asyncio.run(run_ptd_refresh(args))
    if args.command == "promote-definition":
        return asyncio.run(run_ptd_promote(args))
    if args.command == "check":
        return asyncio.run(run_templates_check(args))
    if args.from_file is not None and not args.from_file.is_file():
        sys.stderr.write("templates diff: no such file\n")
        return EXIT_NO_FILE
    return asyncio.run(run_ptd_diff(args))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.group in ("tenant", "user"):
        return asyncio.run(run_tenant_command(args, sys.stdin))
    if args.group == "platform":
        if not args.error_map.is_file():
            sys.stderr.write("platform load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_platform_load(args.error_map))
    if args.group == "errors" and args.command == "catalog":
        return asyncio.run(run_errors_catalog(unmapped=args.unmapped, limit=args.limit))
    if args.group == "errors" and args.command == "export":
        return asyncio.run(run_errors_export(args.format, args.marketplace))
    if args.group == "errors" and args.command == "value-maps":
        return asyncio.run(run_value_maps(max(2, args.min_listings)))
    if args.group == "errors" and args.command == "proposals":
        if args.command_proposals == "list":
            return asyncio.run(run_proposals_list(status=args.status, limit=args.limit))
        return asyncio.run(run_proposal_review(args))
    if args.group == "errors":
        # resolved at call time, so the default is the module constant as it stands
        default = (
            ERROR_MAP_FLIPKART_PATH if args.marketplace == "flipkart" else ERROR_MAP_AMAZON_PATH
        )
        args.file = args.file or default
        if not args.file.is_file():
            sys.stderr.write("errors load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_errors_load(args.file))
    if args.group == "rules":
        if not args.file.is_file():
            sys.stderr.write("rules load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_rules_load(args.file))
    if args.group == "templates" and args.command != "load":
        return _run_ptd(parser, args)
    if args.group == "templates":
        if not args.file.is_file():
            sys.stderr.write("templates load: no such file\n")
            return EXIT_NO_FILE
        return asyncio.run(run_templates_load(args.file))
    if args.group == "dictionary":
        return asyncio.run(run_dictionary_sync(args.file))
    if args.group == "images":
        if args.command == "unpublish":
            return asyncio.run(run_images_unpublish(args.tenant))
        if args.command == "recheck":
            return run_images_recheck_cli(args)
        return asyncio.run(run_images_derive(args.tenant))
    if args.group == "jobs":
        if args.command == "requeue":
            return asyncio.run(run_requeue(args.job_id, args.staff_email, args.reason))
        return asyncio.run(run_dead_letter_list(args.tenant, args.limit))
    if args.group == "seed":
        return asyncio.run(run_seed(args.profile, reset=args.reset))
    if args.group == "ops" and args.command == "amazon-check":
        return run_amazon_check(args)
    if args.group == "ops" and args.command == "amazon-preview":
        return run_amazon_preview(args)
    if args.group == "ops":
        return asyncio.run(run_photo_accuracy(args.days))
    return run_migrate(alembic_config())


def quiet_http_clients() -> None:
    """The HTTP libraries log every request line at INFO, URL and query included: for a PTD
    refresh that is the pre-signed schema link with its signature (definitions.py: never the
    link). The CLI writes its own one-line outcome, so they say nothing below WARNING."""
    from app.core.logging import _HTTP_CLIENT_LOGGERS

    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    quiet_http_clients()
    raise SystemExit(main())
