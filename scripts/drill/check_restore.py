"""The restore drill's checks (S26 T19; AC 8; PLAN D11 as amended by CODE-PLAN C4).

    uv run --frozen python scripts/drill/check_restore.py --source-dsn DSN --restored-dsn DSN \
        --app-rw-password-file FILE [--started EPOCH] [--max-seconds 900]

Compares a `pg_restore` target (a throwaway container from the compose postgres image, bootstrapped
with 0000 and the password file before the restore) with its source, both as the local superuser:

- `alembic_head`: the restored `alembic_version` is the head of apps/api/alembic;
- `table_counts`: every table in `public` (the `procrastinate_*` tables included) has the same
  row count on both sides;
- `procrastinate_objects`: the procrastinate tables and functions are in `public`;
- `forced_rls`: row security is enabled and forced on every table with a `tenant_id` column, and
  on every table the source forces it on;
- `check_helpers_search_path_empty`: every CHECK constraint, re-added under `search_path = ''`
  inside a rolled-back transaction, still validates every row (DATABASE.md:135);
- `app_rw_tcp_login`: `app_rw` logs in over TCP with the password the password file set;
- `app_rw_no_guc_zero_rows`: as that login, with no tenant GUC, a tenant table reads 0 rows;
- `tenant_guc_scoped`: as that login, `app.tenant_id` set, a tenant table reads exactly that
  tenant's rows (the superuser's per-tenant count, 0 foreign rows);
- `token_enc_decrypts`: one `marketplace_accounts.token_enc` opens through
  `app.adapters.credentials.unseal` with the local key;
- `elapsed`: the drill's wall time (from `--started`) is at most `--max-seconds` (L6784: 15 min).

Prints one `drill: <check> ok|FAILED <detail>` line per check (never a row value or a token) and
exits 1 when any check fails. Loopback DSNs only (exit 2 otherwise).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
MAX_SECONDS: Final = 900
PROBE_TABLE: Final = "products"
CHECKS: Final = (
    "alembic_head",
    "table_counts",
    "procrastinate_objects",
    "forced_rls",
    "check_helpers_search_path_empty",
    "app_rw_tcp_login",
    "app_rw_no_guc_zero_rows",
    "tenant_guc_scoped",
    "token_enc_decrypts",
    "elapsed",
)
PROCRASTINATE_TABLES: Final = frozenset(
    {
        "procrastinate_jobs",
        "procrastinate_events",
        "procrastinate_periodic_defers",
        "procrastinate_workers",
    }
)
PROCRASTINATE_FUNCTIONS: Final = frozenset(
    {"procrastinate_defer_jobs_v1", "procrastinate_fetch_job_v2", "procrastinate_retry_job_v2"}
)


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    ok: bool
    detail: str = ""


def plain_dsn(dsn: str) -> str:
    """A libpq DSN: SQLAlchemy's `postgresql+psycopg://` becomes `postgresql://`."""
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def is_local_dsn(dsn: str) -> bool:
    try:
        parts = urlsplit(plain_dsn(dsn))
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "postgresql"
        and parts.hostname in LOCAL_HOSTS
        and (port is None or 1 <= port <= 65535)
    )


def connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(plain_dsn(dsn), connect_timeout=10, autocommit=True)


def alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "apps/api/alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "apps/api/alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("no alembic head")
    return head


def public_tables(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1"
    ).fetchall()
    return [str(r[0]) for r in rows]


def table_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in public_tables(conn):
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", table))
        ).fetchone()
        out[table] = int(row[0]) if row else -1
    return out


def rls_state(conn: psycopg.Connection[Any]) -> dict[str, tuple[bool, bool, bool]]:
    """table -> (has a tenant_id column, row security enabled, forced)."""
    rows = conn.execute(
        "SELECT c.relname, EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid = "
        "c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped), c.relrowsecurity, "
        "c.relforcerowsecurity FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
    ).fetchall()
    return {str(r[0]): (bool(r[1]), bool(r[2]), bool(r[3])) for r in rows}


def check_alembic(restored: psycopg.Connection[Any]) -> Result:
    rows = restored.execute("SELECT version_num FROM public.alembic_version").fetchall()
    head = alembic_head()
    found = [str(r[0]) for r in rows]
    return Result("alembic_head", found == [head], f"restored {found}, head {head}")


def check_counts(source: psycopg.Connection[Any], restored: psycopg.Connection[Any]) -> Result:
    before, after = table_counts(source), table_counts(restored)
    differ = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
    rows = sum(before.values())
    detail = f"{len(before)} tables, {rows} rows" if not differ else f"differ: {differ}"
    return Result("table_counts", not differ and bool(before), detail)


def procrastinate_objects(conn: psycopg.Connection[Any]) -> tuple[set[str], set[str]]:
    """(the procrastinate tables, the procrastinate functions) in `public`."""
    tables = {t for t in public_tables(conn) if t.startswith("procrastinate_")}
    functions = {
        str(r[0])
        for r in conn.execute(
            "SELECT p.proname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
            "ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname LIKE "
            "'procrastinate%'"
        ).fetchall()
    }
    return tables, functions


def check_procrastinate(
    source: psycopg.Connection[Any], restored: psycopg.Connection[Any]
) -> Result:
    src_tables, src_functions = procrastinate_objects(source)
    tables, functions = procrastinate_objects(restored)
    want_tables = PROCRASTINATE_TABLES | src_tables
    want_functions = PROCRASTINATE_FUNCTIONS | src_functions
    missing = sorted((want_tables - tables) | (want_functions - functions))
    return Result(
        "procrastinate_objects",
        not missing,
        f"{len(functions)} functions" if not missing else f"missing {missing}",
    )


def check_rls(source: psycopg.Connection[Any], restored: psycopg.Connection[Any]) -> Result:
    src, dst = rls_state(source), rls_state(restored)
    bad = sorted(
        t
        for t, (tenant, enabled, forced) in dst.items()
        if (tenant and not (enabled and forced))
        or (src.get(t, (False, False, False))[2] and not forced)
    )
    forced = sum(1 for _, (_, _, f) in dst.items() if f)
    return Result("forced_rls", not bad and forced > 0, f"{forced} forced" if not bad else str(bad))


def check_constraints_search_path(restored_dsn: str) -> Result:
    """Every CHECK constraint re-added under `search_path = ''`, rolled back (DATABASE.md:135)."""
    with psycopg.connect(plain_dsn(restored_dsn), connect_timeout=10) as conn:
        conn.execute("SET search_path = ''")
        rows = conn.execute(
            "SELECT n.nspname, c.relname, k.conname, pg_catalog.pg_get_constraintdef(k.oid) "
            "FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid = k.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE k.contype = 'c' AND n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "AND k.conislocal ORDER BY 2, 3"
        ).fetchall()
        failed: list[str] = []
        for schema, table, name, definition in rows:
            target = sql.Identifier(str(schema), str(table))
            try:
                with conn.transaction(force_rollback=True):
                    conn.execute(
                        sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                            target, sql.Identifier(str(name))
                        )
                    )
                    conn.execute(
                        sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} {}").format(
                            target, sql.Identifier(str(name)), sql.SQL(str(definition))
                        )
                    )
            except psycopg.Error as exc:
                failed.append(f"{table}.{name} ({exc.sqlstate})")
    return Result(
        "check_helpers_search_path_empty",
        not failed and bool(rows),
        f"{len(rows)} CHECK constraints" if not failed else str(failed),
    )


def probe_tenant(restored: psycopg.Connection[Any]) -> tuple[uuid.UUID, int, int]:
    """(the tenant with the most probe-table rows, its row count, every other tenant's rows)."""
    rows = restored.execute(
        sql.SQL("SELECT tenant_id, count(*) FROM {} GROUP BY 1 ORDER BY 2 DESC, 1").format(
            sql.Identifier("public", PROBE_TABLE)
        )
    ).fetchall()
    if not rows:
        raise RuntimeError(f"no {PROBE_TABLE} rows to probe")
    tenant, own = rows[0]
    return uuid.UUID(str(tenant)), int(own), sum(int(r[1]) for r in rows[1:])


def check_app_rw(
    restored_dsn: str, password: str, superuser: psycopg.Connection[Any]
) -> list[Result]:
    parts = urlsplit(plain_dsn(restored_dsn))
    try:  # TCP to the published port, the password the password file set (never a socket)
        login = psycopg.connect(
            host=parts.hostname or "127.0.0.1",
            port=parts.port or 5432,
            dbname=parts.path.lstrip("/"),
            user="app_rw",
            password=password,
            connect_timeout=10,
        )
    except psycopg.OperationalError as exc:
        failed = Result("app_rw_tcp_login", False, f"login refused ({type(exc).__name__})")
        return [
            failed,
            Result("app_rw_no_guc_zero_rows", False),
            Result("tenant_guc_scoped", False),
        ]
    out = [Result("app_rw_tcp_login", True, "TCP login as app_rw")]
    tenant, own, foreign = probe_tenant(superuser)
    table = sql.Identifier("public", PROBE_TABLE)
    with login:
        row = login.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()
        unscoped = int(row[0]) if row else -1
        login.rollback()
        out.append(Result("app_rw_no_guc_zero_rows", unscoped == 0, f"{unscoped} rows unscoped"))
        login.execute("SELECT pg_catalog.set_config('app.tenant_id', %s, true)", [str(tenant)])
        row = login.execute(
            sql.SQL("SELECT count(*), count(*) FILTER (WHERE tenant_id <> %s) FROM {}").format(
                table
            ),
            [tenant],
        ).fetchone()
        login.rollback()
    seen, other = (int(row[0]), int(row[1])) if row else (-1, -1)
    ok = seen == own and other == 0 and (foreign == 0 or seen < own + foreign)
    out.append(Result("tenant_guc_scoped", ok, f"{seen} of {own} own rows, {other} foreign"))
    return out


def check_token(restored: psycopg.Connection[Any]) -> Result:
    from app.adapters.credentials import get_key_unwrapper, unseal
    from app.core.config import get_settings

    row = restored.execute(
        "SELECT tenant_id, token_enc, kms_key_version FROM public.marketplace_accounts "
        "WHERE token_enc IS NOT NULL ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return Result("token_enc_decrypts", False, "no sealed marketplace account")
    tenant, blob, version = uuid.UUID(str(row[0])), bytes(row[1]), str(row[2])
    try:
        pair = asyncio.run(
            unseal(
                blob,
                key_version=version,
                tenant_id=tenant,
                unwrapper=get_key_unwrapper(get_settings()),
            )
        )
    except Exception as exc:  # the class only: the error never carries token bytes
        return Result("token_enc_decrypts", False, type(exc).__name__)
    opened = bool(pair.access_token.get_secret_value()) and bool(
        pair.refresh_token.get_secret_value()
    )
    return Result("token_enc_decrypts", opened, "one account's token pair opened")


def run_checks(
    source_dsn: str,
    restored_dsn: str,
    app_rw_password: str,
    *,
    started: float | None = None,
    max_seconds: int = MAX_SECONDS,
    clock: Callable[[], float] = time.time,
) -> list[Result]:
    results: list[Result] = []
    with connect(source_dsn) as source, connect(restored_dsn) as restored:
        results.append(check_alembic(restored))
        results.append(check_counts(source, restored))
        results.append(check_procrastinate(source, restored))
        results.append(check_rls(source, restored))
        results.append(check_constraints_search_path(restored_dsn))
        results.extend(check_app_rw(restored_dsn, app_rw_password, restored))
        results.append(check_token(restored))
    results.append(check_elapsed(started, max_seconds, clock))
    return results


def check_elapsed(
    started: float | None, max_seconds: int, clock: Callable[[], float] = time.time
) -> Result:
    if started is None:
        return Result("elapsed", True, "not timed")
    seconds = clock() - started
    return Result("elapsed", seconds <= max_seconds, f"{seconds:.0f} s (max {max_seconds} s)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/drill/check_restore.py")
    parser.add_argument("--source-dsn", required=True)
    parser.add_argument("--restored-dsn", required=True)
    parser.add_argument("--app-rw-password-file", type=Path, required=True)
    parser.add_argument("--started", type=float, default=None)
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    args = parser.parse_args(argv)
    for dsn in (args.source_dsn, args.restored_dsn):
        if not is_local_dsn(dsn):
            print("drill: refused: the DSNs must point at 127.0.0.1 or localhost")
            return 2
    password = args.app_rw_password_file.read_text(encoding="utf-8").rstrip("\n")
    results = run_checks(
        args.source_dsn,
        args.restored_dsn,
        password,
        started=args.started,
        max_seconds=args.max_seconds,
    )
    for r in results:
        print(f"drill: {r.name} {'ok' if r.ok else 'FAILED'} {r.detail}".rstrip())
    failed = [r.name for r in results if not r.ok]
    print(f"drill: {'FAILED ' + ','.join(failed) if failed else 'all checks ok'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
