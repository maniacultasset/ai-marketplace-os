"""S26 T19 (AC 8; CODE-PLAN C4): a `pg_dump -Fc` of a migrated, seeded database restored into a
throwaway container from the compose postgres image, after 0000 and the password file, passes
every scripts/drill/check_restore.py check. Marker `drill`: `uv run --frozen pytest -m drill`
(never in api-tests). The source is its own database `mos_drill` on the test cluster (never
mos_test or mos_iso); the container and the database are removed afterwards, even on failure."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
import yaml
from pydantic import SecretStr

pytestmark = [pytest.mark.drill, pytest.mark.allow_hosts(["127.0.0.1", "localhost"])]

REPO_ROOT = Path(__file__).resolve().parents[4]
DRILL_DB = "mos_drill"
READY_TIMEOUT_S = 60


def _checks() -> ModuleType:
    if "check_restore" in sys.modules:
        return sys.modules["check_restore"]
    spec = importlib.util.spec_from_file_location(
        "check_restore", REPO_ROOT / "scripts/drill/check_restore.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_restore"] = module
    spec.loader.exec_module(module)
    return module


def _cluster_port() -> int:
    url = os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@localhost:5432/mos_test")
    return urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1)).port or 5432


def _admin_dsn(db: str) -> str:
    return f"postgresql://postgres:postgres@127.0.0.1:{_cluster_port()}/{db}"


def _run(*args: str, stdin: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        list(args),
        input=stdin,
        capture_output=True,
        timeout=600,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise AssertionError(f"{args[0]} {args[1]} failed: {result.stderr.decode()[-2000:]}")
    return result.stdout


def _source_container() -> str:
    """The container publishing the test cluster's port (the checkout's compose postgres)."""
    names = _run("docker", "ps", "--filter", f"publish={_cluster_port()}", "--format", "{{.Names}}")
    found = [n for n in names.decode().split() if n]
    assert len(found) == 1, f"one container must publish {_cluster_port()}, found {found}"
    return found[0]


def _cli(*args: str) -> None:
    dsn = _admin_dsn(DRILL_DB).replace("postgresql://", "postgresql+psycopg://", 1)
    _run(
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "app.cli",
        *args,
        env={
            "APP_ENV": "local",
            "APP_ROLE": "migrate",
            "DATABASE_URL": dsn,
            "ALEMBIC_DATABASE_URL": dsn,
        },
    )


def _seal_blob(tenant: uuid.UUID) -> tuple[bytes, str]:
    from app.core.config import Settings
    from app.core.crypto import LocalKmsStub, TokenPair, seal

    key = Settings.model_fields["kms_local_dev_key"].default
    pair = TokenPair(
        access_token=SecretStr("drill-" + secrets.token_hex(8)),
        refresh_token=SecretStr("drill-" + secrets.token_hex(8)),
    )
    sealed = asyncio.run(seal(pair, tenant_id=tenant, wrapper=LocalKmsStub(key)))
    return sealed.blob, sealed.key_version


def _seed_source() -> None:
    with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DRILL_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DRILL_DB}")
    with psycopg.connect(_admin_dsn(DRILL_DB), autocommit=True) as conn:
        conn.execute((REPO_ROOT / "scripts/db/0000_bootstrap.sql").read_text(encoding="utf-8"))
    dsn = _admin_dsn(DRILL_DB).replace("postgresql://", "postgresql+psycopg://", 1)
    _run(
        "uv",
        "run",
        "--frozen",
        "alembic",
        "-c",
        str(REPO_ROOT / "apps/api/alembic.ini"),
        "upgrade",
        "head",
        env={"APP_ENV": "local", "DATABASE_URL": dsn, "ALEMBIC_DATABASE_URL": dsn},
    )
    _cli("dictionary", "sync")
    _cli("platform", "load")
    _cli("seed", "--profile", "demo")
    with psycopg.connect(_admin_dsn(DRILL_DB)) as conn:
        tenant, user = conn.execute(
            "SELECT m.tenant_id, m.user_id FROM tenant_memberships m JOIN tenants t "
            "ON t.id = m.tenant_id WHERE t.slug = 'acme' AND m.role = 'owner'"
        ).fetchone() or (None, None)
        assert tenant is not None
        blob, version = _seal_blob(uuid.UUID(str(tenant)))
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
        conn.execute(
            "INSERT INTO marketplace_accounts (tenant_id, marketplace, seller_id, scopes, "
            "token_enc, kms_key_version, connected_by_user_id) VALUES (%s, 'amazon', %s, "
            "'{}', %s, %s, %s)",
            [tenant, "DRILL" + secrets.token_hex(4).upper(), blob, version, user],
        )
        conn.commit()


def _image() -> str:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return str(compose["services"]["postgres"]["image"])


def _wait_ready(name: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise AssertionError(f"the throwaway postgres was not ready in {READY_TIMEOUT_S} s")


def _verifier(password: str) -> str:
    out = _run(
        "uv",
        "run",
        "--frozen",
        "python",
        str(REPO_ROOT / "scripts/db/scram_verifier.py"),
        stdin=password.encode(),
    )
    return out.decode().strip()


@pytest.fixture(scope="module")
def drill() -> Iterator[dict[str, Any]]:
    name = f"mkos-drill-test-{secrets.token_hex(4)}"
    superuser = secrets.token_urlsafe(24)
    app_rw = secrets.token_urlsafe(24)
    try:
        _seed_source()
        dump = _run(
            "docker", "exec", _source_container(), "pg_dump", "-U", "postgres", "-Fc", DRILL_DB
        )
        _run(
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "POSTGRES_DB=marketplace_os",
            _image(),
            env={"POSTGRES_PASSWORD": superuser},
        )
        _wait_ready(name)
        port = int(_run("docker", "port", name, "5432/tcp").decode().split()[0].rsplit(":", 1)[1])
        psql = ("docker", "exec", "-i")
        target = (name, "psql", "-U", "postgres", "-d", "marketplace_os", "-v", "ON_ERROR_STOP=1")
        bootstrap = (REPO_ROOT / "scripts/db/0000_bootstrap.sql").read_bytes()
        _run(*psql, *target, "-X", "-q", "-f", "-", stdin=bootstrap)
        passwords = (REPO_ROOT / "scripts/db/0000_role_passwords.sql").read_bytes()
        _run(
            *psql,
            "-e",
            "APP_RW_SCRAM",
            *target,
            "-X",
            "-q",
            "-f",
            "-",
            stdin=passwords,
            env={"APP_RW_SCRAM": _verifier(app_rw)},
        )
        _run(
            "docker",
            "exec",
            "-i",
            name,
            "pg_restore",
            "-U",
            "postgres",
            "-d",
            "marketplace_os",
            "--exit-on-error",
            stdin=dump,
        )
        os.environ["PGPASSWORD"] = superuser
        results = _checks().run_checks(
            _admin_dsn(DRILL_DB), f"postgresql://postgres@127.0.0.1:{port}/marketplace_os", app_rw
        )
        yield {r.name: r for r in results}
    finally:
        os.environ.pop("PGPASSWORD", None)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {DRILL_DB} WITH (FORCE)")


def test_pg_dump_restore_round_trip(drill: dict[str, Any]) -> None:
    assert set(drill) == set(_checks().CHECKS)
    failed = {name: r.detail for name, r in drill.items() if not r.ok}
    assert failed == {}


def test_restore_check_helpers_with_empty_search_path(drill: dict[str, Any]) -> None:
    assert drill["check_helpers_search_path_empty"].ok, drill["check_helpers_search_path_empty"]


def test_restore_app_rw_tcp_login(drill: dict[str, Any]) -> None:
    for name in ("app_rw_tcp_login", "app_rw_no_guc_zero_rows", "tenant_guc_scoped"):
        assert drill[name].ok, drill[name]


def test_restore_procrastinate_objects_in_public(drill: dict[str, Any]) -> None:
    assert drill["procrastinate_objects"].ok, drill["procrastinate_objects"]
    assert drill["table_counts"].ok, drill["table_counts"]


def test_restore_token_enc_decrypts_with_local_key(drill: dict[str, Any]) -> None:
    assert drill["token_enc_decrypts"].ok, drill["token_enc_decrypts"]
