"""The Cloud SQL mimic (DATABASE.md:1754, open decision 3): a throwaway container from the compose
postgres image, where 0000 runs as a non-superuser CREATEROLE role that owns the database, the way
the Cloud SQL `postgres` user runs it. `make bootstrap-mimic` runs this suite; `make api-tests` does
not. The container is removed afterwards, even when setup fails."""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BOOTSTRAP_SQL = REPO_ROOT / "scripts/db/0000_bootstrap.sql"
READY_TIMEOUT_S = 60
FIELD_SEP = "\x1f"


def compose_postgres_image() -> str:
    """The one image pin: docker-compose.yml's postgres service."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return str(compose["services"]["postgres"]["image"])


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Mimic:
    """A running mimic container. `boot` is the CREATEROLE non-superuser that owns `mimic`;
    `ref` is the same file run by the superuser, for comparison."""

    name: str
    host_port: int
    bootstrap_runs: dict[str, list[Run]] = field(default_factory=dict)

    def run_sql_file(
        self,
        sql: str,
        *,
        db: str = "mimic",
        user: str = "boot",
        env: Mapping[str, str] | None = None,
    ) -> Run:
        """Run SQL through psql inside the container, on stdin. `env` values reach the container
        by name (`docker exec -e NAME`), never on a command line."""
        names = [arg for name in (env or {}) for arg in ("-e", name)]
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                *names,
                self.name,
                "psql",
                "-h",
                "127.0.0.1",
                *_psql_args(user, db),
            ],
            input=sql,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=_child_env(env),
        )
        return Run(result.returncode, result.stdout, result.stderr)

    def psql(self, sql: str, *, db: str = "mimic", user: str = "postgres") -> list[tuple[str, ...]]:
        """Rows of one query, every value as text (NULL as the empty string)."""
        result = subprocess.run(
            [
                *("docker", "exec", "-i", self.name, "psql", "-h", "127.0.0.1"),
                *_psql_args(user, db),
                *("-A", "-t", "-F", FIELD_SEP),
            ],
            input=sql,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"mimic psql failed ({result.returncode}): {result.stderr}")
        return [tuple(line.split(FIELD_SEP)) for line in result.stdout.splitlines() if line]

    def logs(self) -> str:
        result = subprocess.run(
            ["docker", "logs", self.name], capture_output=True, text=True, timeout=60, check=True
        )
        return result.stdout + result.stderr


def _psql_args(user: str, db: str) -> list[str]:
    """Connection and safety flags; the SQL always arrives on stdin (`-f -`)."""
    return ["-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-X", "-q", "-f", "-"]


def _child_env(extra: Mapping[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return None
    # The docker CLI still needs PATH, HOME and DOCKER_* from this process.
    return {**os.environ, **extra}


def _docker(*args: str, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=_child_env(env),
    )
    if result.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def _wait_ready(name: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
    tail = subprocess.run(
        ["docker", "logs", "--tail", "40", name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    raise AssertionError(f"mimic postgres not ready in {READY_TIMEOUT_S}s:\n{tail.stderr}")


def _start(name: str, *server_args: str) -> Mimic:
    """A container from the compose image with `boot` (CREATEROLE, not superuser) owning
    `mimic`, where the Cloud SQL superuser-equivalent has created `vector` (it is untrusted)."""
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_PASSWORD",
        compose_postgres_image(),
        *server_args,
        env={"POSTGRES_PASSWORD": secrets.token_urlsafe(24)},
    )
    _wait_ready(name)
    port = int(_docker("port", name, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
    box = Mimic(name=name, host_port=port)
    box.psql("CREATE ROLE boot LOGIN CREATEROLE", db="postgres")
    box.psql("CREATE DATABASE mimic OWNER boot", db="postgres")
    box.psql("CREATE EXTENSION vector", db="mimic")
    return box


@pytest.fixture(scope="session")
def mimic() -> Iterator[Mimic]:
    name = f"mkos-s5-mimic-{secrets.token_hex(4)}"
    try:
        box = _start(name, "-c", "log_statement=all")
        box.psql("CREATE DATABASE ref", db="postgres")
        sql = BOOTSTRAP_SQL.read_text(encoding="utf-8")
        box.bootstrap_runs["mimic"] = [box.run_sql_file(sql, db="mimic", user="boot")]
        box.bootstrap_runs["mimic"].append(box.run_sql_file(sql, db="mimic", user="boot"))
        box.bootstrap_runs["ref"] = [box.run_sql_file(sql, db="ref", user="postgres")]
        box.bootstrap_runs["ref"].append(box.run_sql_file(sql, db="ref", user="postgres"))
        yield box
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)


@pytest.fixture(scope="session")
def pgss_mimic() -> Iterator[Mimic]:
    """The mimic with pg_stat_statements preloaded at its defaults (track_utility on), as Cloud
    SQL Query Insights runs it; 0000 has run once as `boot`."""
    name = f"mkos-s5-pgss-{secrets.token_hex(4)}"
    try:
        box = _start(name, "-c", "shared_preload_libraries=pg_stat_statements")
        run = box.run_sql_file(BOOTSTRAP_SQL.read_text(encoding="utf-8"), db="mimic", user="boot")
        if run.returncode != 0:
            raise AssertionError(f"pgss mimic bootstrap exited {run.returncode}: {run.stderr}")
        box.psql("CREATE EXTENSION pg_stat_statements", db="mimic")
        yield box
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
