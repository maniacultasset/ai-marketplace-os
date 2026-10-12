"""0000 on Cloud SQL without psql: `scripts/deploy/gcp/deploy.sh db-init` runs this in a one-off
Cloud Run job of the API image (APP_ROLE=shell; the image carries scripts/db/ but no psql).

    python /src/scripts/db/run_bootstrap.py --host /cloudsql/<PROJECT>:<REGION>:<INSTANCE> \
        --dbname marketplace_os --user postgres --password-file /secrets/postgres/value \
        [--app-rw-password-file F] [--app-ro-password-file F] [--migrator-password-file F]

1. `0000_bootstrap.sql` as one simple-query batch (idempotent, so a rerun is safe), as the login
   given: on Cloud SQL the `postgres` user, a CREATEROLE non-superuser that owns the database.
2. The role passwords, the same way `0000_role_passwords.sql` sets them under psql: a SCRAM
   verifier per role computed here (`scram_verifier.py`), so no plaintext reaches the server; the
   file's own `lmes_settable` and `pw_safe` queries, read from it at run time so the two never
   drift (the server logs no statement and no failing statement, and every role is alterable); one
   transaction that first takes each target row with a no-op CONNECTION LIMIT, then the PASSWORD
   ALTERs. A role whose file is not given is left alone.

Passwords come from files (Cloud Run mounts each secret as one), never argv or the environment.
Output is fixed lines; an error names its class and SQLSTATE only, never a password, a verifier
or the driver's message.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import psycopg
from psycopg import sql

HERE: Final = Path(__file__).resolve().parent
BOOTSTRAP_SQL: Final = HERE / "0000_bootstrap.sql"
PASSWORDS_SQL: Final = HERE / "0000_role_passwords.sql"
ROLES: Final = ("app_rw", "app_ro", "migrator")
CONNECT_TIMEOUT_S: Final = 10
# A query of the password file: from a line starting `SELECT` to its `AS <name> \gset`, never
# across another `\gset`.
_PSQL_QUERY: Final = r"^(SELECT\s(?:(?!\\gset).)*?)\s+AS {name} \\gset"


def _scram() -> ModuleType:
    """`scram_verifier.py` beside this file, loaded by path (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("scram_verifier", HERE / "scram_verifier.py")
    if spec is None or spec.loader is None:
        raise ImportError("scram_verifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def psql_query(text: str, name: str) -> str:
    """The `SELECT ... AS <name> \\gset` query of the password file, as plain SQL."""
    match = re.search(_PSQL_QUERY.format(name=name), text, re.DOTALL | re.MULTILINE)
    if match is None:
        raise ValueError(f"0000_role_passwords.sql has no {name} query")
    return f"{match.group(1)} AS {name}"


def read_secret(path: Path) -> str:
    """A mounted secret: the file, minus one trailing newline, as printable ASCII."""
    return str(_scram().read_password(path.open(encoding="ascii", newline="")))


def verifiers(files: dict[str, Path | None]) -> dict[str, str]:
    scram = _scram()
    return {
        role: str(scram.scram_sha256_verifier(read_secret(path)))
        for role, path in files.items()
        if path is not None
    }


def bootstrap(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    conn.execute(BOOTSTRAP_SQL.read_text(encoding="utf-8").encode())


def set_passwords(conn: psycopg.Connection[tuple[object, ...]], scram: dict[str, str]) -> bool:
    """False when the server could log or keep a statement carrying a verifier; nothing is set."""
    text = PASSWORDS_SQL.read_text(encoding="utf-8")
    settable = conn.execute(psql_query(text, "lmes_settable").encode()).fetchone()
    if settable is not None and settable[0]:
        conn.execute("SET log_min_error_statement = panic")
    safe = conn.execute(psql_query(text, "pw_safe").encode()).fetchone()
    if safe is None or not safe[0]:
        return False
    with conn.transaction():
        limits = dict(
            conn.execute(
                "SELECT rolname, rolconnlimit FROM pg_roles WHERE rolname = ANY(%s)", [list(scram)]
            ).fetchall()
        )
        for role in scram:
            conn.execute(
                sql.SQL("ALTER ROLE {} CONNECTION LIMIT {}").format(
                    sql.Identifier(role), sql.Literal(limits[role])
                )
            )
        for role, verifier in scram.items():
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(verifier)
                )
            )
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_bootstrap.py", description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="a socket dir (/cloudsql/...) or a host")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    for role in ROLES:
        parser.add_argument(f"--{role.replace('_', '-')}-password-file", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    files = {role: getattr(args, f"{role}_password_file") for role in ROLES}
    try:
        login = read_secret(args.password_file)
        scram = verifiers(files)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"run_bootstrap: a password file is unreadable ({type(exc).__name__})\n")
        return 2
    try:
        with psycopg.connect(
            host=args.host,
            port=args.port,
            dbname=args.dbname,
            user=args.user,
            password=login,
            connect_timeout=CONNECT_TIMEOUT_S,
            application_name="mkos-bootstrap",
            autocommit=True,
        ) as conn:
            bootstrap(conn)
            sys.stdout.write("run_bootstrap: 0000_bootstrap.sql OK\n")
            if not scram:
                sys.stdout.write("run_bootstrap: no role password given\n")
                return 0
            if not set_passwords(conn, scram):
                sys.stderr.write(
                    "run_bootstrap: the server logs or keeps statements or failing statements, "
                    "or a target role is missing or not alterable; no password was set\n"
                )
                return 1
    except psycopg.Error as exc:
        sys.stderr.write(f"run_bootstrap: {type(exc).__name__} (SQLSTATE {exc.sqlstate})\n")
        return 1
    sys.stdout.write(f"run_bootstrap: role passwords set for {', '.join(scram)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
