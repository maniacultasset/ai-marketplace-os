"""AC11: the API image files (ADR-0002, ADR-0005, CANON-ADDENDUM.md §15; nested secret globs)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

DIGEST = r"@sha256:[0-9a-f]{64}"


@pytest.fixture
def dockerfile(repo_root: Path) -> str:
    return (repo_root / "apps/api/Dockerfile").read_text(encoding="utf-8")


@pytest.fixture
def entrypoint(repo_root: Path) -> Path:
    return repo_root / "apps/api/docker-entrypoint.sh"


def _instructions(dockerfile: str) -> list[str]:
    """Dockerfile instructions with continuation lines joined and comments dropped."""
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    return [
        line.strip() for line in joined.splitlines() if line.strip() and not line.startswith("#")
    ]


def test_base_image_python_313_slim_trixie_pinned(dockerfile: str) -> None:
    froms = [i for i in _instructions(dockerfile) if i.startswith("FROM ")]
    assert re.fullmatch(rf"FROM python:3\.13-slim-trixie{DIGEST} AS base", froms[0])
    assert froms[1:] == ["FROM base AS builder", "FROM base AS runtime"]


def test_uv_image_0_12_19(dockerfile: str) -> None:
    copies = [i for i in _instructions(dockerfile) if "astral-sh/uv" in i]
    assert len(copies) == 1
    assert re.fullmatch(
        rf"COPY --from=ghcr\.io/astral-sh/uv:0\.12\.19{DIGEST} /uv /usr/local/bin/uv", copies[0]
    )


def test_builder_sets_uv_project_environment(dockerfile: str) -> None:
    instructions = _instructions(dockerfile)
    builder = instructions.index("FROM base AS builder")
    assert instructions[builder + 1] == "ENV UV_PROJECT_ENVIRONMENT=/opt/venv"


def test_every_uv_sync_builds_wheels_not_editables(dockerfile: str) -> None:
    """Fix round 1 (F3, F4): for an editable build, hatchling's `get_requires_for_build_editable`
    adds `editables~=0.3`, which uv then fetches from PyPI unpinned and unhashed into the image
    builder. A wheel build needs only the pinned build constraints (uv.lock [manifest])."""
    commands = [
        command.strip()
        for instruction in _instructions(dockerfile)
        if instruction.startswith("RUN ")
        for command in instruction.removeprefix("RUN ").split("&&")
        if "uv sync" in command
    ]
    assert len(commands) == 2
    for command in commands:
        assert "--no-editable" in command.split(), command


def test_no_gunicorn_or_web_concurrency(dockerfile: str, entrypoint: Path) -> None:
    for text in (dockerfile, entrypoint.read_text(encoding="utf-8")):
        assert "gunicorn" not in text.lower()
        assert "WEB_CONCURRENCY" not in text
        assert "UvicornWorker" not in text


def test_copies_only_schemas_stub_api_and_scripts_db(dockerfile: str) -> None:
    sources = []
    for instruction in _instructions(dockerfile):
        if instruction.startswith("COPY ") and "--from=" not in instruction:
            words = [w for w in instruction.split()[1:] if not w.startswith("--")]
            sources.extend(words[:-1])
    assert sources == [
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "packages/schemas/pyproject.toml",
        "packages/pipeline-stub/pyproject.toml",
        "apps/api/pyproject.toml",
        "packages/schemas/",
        "packages/pipeline-stub/",
        "apps/api/",
        "scripts/db/",
        "apps/api/docker-entrypoint.sh",
    ]
    assert "build-essential" not in dockerfile
    # S14b T1: the in-repo stub ships in the image (ADR-0013 §4); the AI team's package never does.
    assert "packages/pipeline/" not in dockerfile


def test_runtime_uid_10001(dockerfile: str) -> None:
    instructions = _instructions(dockerfile)
    runtime = instructions[instructions.index("FROM base AS runtime") :]
    assert any(re.search(r"useradd .*--uid 10001\b", i) for i in runtime)
    assert "USER app" in runtime
    assert "ENV APP_ROLE=api PORT=8080 PYTHONPATH=/src/apps/api" in runtime
    assert "EXPOSE 8080" in runtime


def _run_entrypoint(entrypoint: Path, role: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "APP_ROLE": role, "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
    return subprocess.run(
        ["sh", str(entrypoint), *args], env=env, capture_output=True, text=True, check=False
    )


def test_entrypoint_roles(entrypoint: Path) -> None:
    assert subprocess.run(["sh", "-n", str(entrypoint)], check=False).returncode == 0
    bogus = _run_entrypoint(entrypoint, "bogus")
    assert bogus.returncode == 64
    assert "api|worker|migrate|seed|shell" in bogus.stderr
    shell = _run_entrypoint(entrypoint, "shell", "-c", "print(1)")
    assert shell.returncode == 0, shell.stderr
    assert shell.stdout == "1\n"


def test_entrypoint_migrate_role_runs_the_cli(entrypoint: Path, tmp_path: Path) -> None:
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "APP_ROLE": "migrate",
        "APP_ENV": "local",
        "ALEMBIC_DATABASE_URL": "postgresql+psycopg://leakuser_s5:leakpw_s5@127.0.0.1:1/x",
    }
    result = subprocess.run(
        ["sh", str(entrypoint)],
        env=env,
        cwd=tmp_path,  # no developer .env
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith("migrate: failed (OperationalError")
    assert result.stderr.count("\n") == 1
    for token in ("leakuser_s5", "leakpw_s5", "127.0.0.1", "@"):
        assert token not in result.stdout + result.stderr, token


def _role_block(entrypoint: Path, role: str) -> list[str]:
    text = entrypoint.read_text(encoding="utf-8")
    body = text.split(f"  {role})\n", 1)[1].split("    ;;", 1)[0]
    return [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_entrypoint_migrate_role_loads_platform_data_in_order(entrypoint: Path) -> None:
    """The migrate job: the schema, the dictionary the facts guard reads, then the platform rules
    before the templates (generation is gated on a current template only, so a failed rules load
    must leave no category generatable). No path arguments (the CLI's defaults ship in the image)
    and no shell variables (`set -eu`); only the last command execs."""
    assert _role_block(entrypoint, "migrate") == [
        "python -m app.cli db migrate",
        "python -m app.cli dictionary sync",
        "python -m app.cli rules load",
        "exec python -m app.cli templates load",
    ]


def test_dockerignore_secret_globs_nested_and_ordered(repo_root: Path) -> None:
    lines = [
        line.strip()
        for line in (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for pattern in (
        "**/.env",
        "**/.env.*",
        "**/secrets/",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/gcp-sa-*.json",
    ):
        assert pattern in lines, pattern
    assert lines.index("!**/.env.example") > lines.index("**/.env.*")
    assert not [line for line in lines if line.lstrip("!").startswith((".env", "*.pem", "*.key"))]
    assert not [line for line in lines if "apps/web" in line]
    for excluded in ("docs/", "deliverables/", "scaffold/", "scripts/docgen/", "scripts/trace/"):
        assert excluded in lines, excluded
    assert ".tools/" in lines


def test_entrypoint_api_role_trusts_proxy_headers(entrypoint: Path) -> None:
    text = entrypoint.read_text(encoding="utf-8")
    api = re.search(r"^\s*api\)\n(.*?);;", text, flags=re.MULTILINE | re.DOTALL)
    assert api is not None
    command = [line for line in api.group(1).splitlines() if "exec " in line]
    assert len(command) == 1
    assert command[0].split() == [
        "exec",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",  # noqa: S104 - the container must listen on every interface
        "--port",
        '"${PORT:-8080}"',
        "--proxy-headers",
        "--forwarded-allow-ips='*'",
        "--no-access-log",
        "--no-server-header",
    ]


def test_dockerignore_excludes_mock(repo_root: Path) -> None:
    # S3 D5: the local mock is dev tooling; the Dockerfile copies apps/api, so .dockerignore drops it.
    lines = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "apps/api/app_mock/" in lines


def test_entrypoint_seed_role_verbatim(entrypoint: Path) -> None:
    """CANON-ADDENDUM.md A§5: the seed role line, verbatim."""
    text = entrypoint.read_text(encoding="utf-8")
    assert '    exec python -m app.cli seed --profile "${SEED_PROFILE:-demo}"\n' in text
    assert "\n  seed)\n" in text


def test_entrypoint_unknown_role_lists_seed(entrypoint: Path) -> None:
    bogus = _run_entrypoint(entrypoint, "bogus")
    assert bogus.returncode == 64
    assert "unknown APP_ROLE='bogus' (api|worker|migrate|seed|shell)" in bogus.stderr


def test_entrypoint_no_access_log(entrypoint: Path) -> None:
    # S11: the app writes the one access line per request (request id, route template, no query
    # string); uvicorn's own access line would add a second, id-less one.
    [line] = [
        line
        for line in entrypoint.read_text(encoding="utf-8").splitlines()
        if "exec uvicorn" in line
    ]
    assert "--no-access-log" in line.split()
    # the API does not announce its server software
    assert line.rstrip().endswith("--no-server-header")


def test_image_excludes_tests_dir(repo_root: Path) -> None:
    """S15: the fake marketplace and every other test helper stay out of the image (the
    Dockerfile copies apps/api/, so .dockerignore drops its tests)."""
    lines = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "apps/api/tests/" in lines
    app_sources = "\n".join(
        p.read_text(encoding="utf-8") for p in (repo_root / "apps/api/app").rglob("*.py")
    )
    assert "fake_marketplace" not in app_sources
