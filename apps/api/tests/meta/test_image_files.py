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
    assert froms[1:] == [
        "FROM base AS builder",
        "FROM base AS pipeline-src",
        "FROM builder AS builder-pipeline",
        "FROM builder${PIPELINE_REF:+-pipeline} AS venv",
        "FROM base AS runtime",
    ]


# ---- ADR-0024: the real pipeline behind PIPELINE_REF ----

# GitHub's published ed25519 host key fingerprint (`gh api meta`, ssh_key_fingerprints).
GITHUB_ED25519_SHA256 = "+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"


def _stage(dockerfile: str, name: str) -> list[str]:
    instructions = _instructions(dockerfile)
    start = next(i for i, line in enumerate(instructions) if line.endswith(f" AS {name}"))
    end = next(
        (i for i in range(start + 1, len(instructions)) if instructions[i].startswith("FROM ")),
        len(instructions),
    )
    return instructions[start:end]


def test_pipeline_ref_defaults_empty_and_selects_the_stub(dockerfile: str) -> None:
    """Empty PIPELINE_REF (CI, local, every build so far) builds the stub image: the runtime
    copies `venv`, which is `builder` unless the arg is set."""
    instructions = _instructions(dockerfile)
    assert instructions[0] == 'ARG PIPELINE_REF=""'
    runtime = _stage(dockerfile, "runtime")
    copies = [i for i in runtime if i.startswith("COPY --from=") and "astral-sh" not in i]
    assert copies[:2] == ["COPY --from=venv /opt/venv /opt/venv", "COPY --from=venv /src /src"]
    assert not [i for i in runtime if "PIPELINE_REF" in i or "pipeline" in i.lower()]


def test_pipeline_source_pinned_by_sha_over_a_pinned_host_key(dockerfile: str) -> None:
    import base64
    import hashlib

    stage = " ".join(_stage(dockerfile, "pipeline-src"))
    hosts = re.findall(r"'github\.com ssh-ed25519 ([A-Za-z0-9+/=]+)'", stage)
    assert len(hosts) == 1
    digest = hashlib.sha256(base64.b64decode(hosts[0])).digest()
    assert base64.b64encode(digest).decode().rstrip("=") == GITHUB_ED25519_SHA256
    assert "StrictHostKeyChecking=yes" in stage and "StrictHostKeyChecking=no" not in stage
    assert "HostKeyAlgorithms=ssh-ed25519" in stage and "BatchMode=yes" in stage
    assert "grep -Eqx '[0-9a-f]{40}'" in stage
    assert 'test "$(git -C /pipeline rev-parse HEAD)" = "$PIPELINE_REF"' in stage
    assert "git@github.com:maniacultasset/mkos-pipeline.git" in stage
    assert "rm -rf /pipeline/.git" in stage


def test_pipeline_key_is_a_secret_mount_only(dockerfile: str) -> None:
    """The deploy key is a BuildKit secret for one RUN: never a build arg, ENV, COPY or file."""
    mounts = re.findall(r"--mount=type=secret,[^ ]+", dockerfile)
    assert mounts == ["--mount=type=secret,id=mkos_pipeline_key,required=true"]
    for instruction in _instructions(dockerfile):
        if instruction.startswith(("ARG ", "ENV ", "COPY ", "LABEL ")):
            assert "key" not in instruction.lower() or "astral-sh" in instruction, instruction
    assert "/run/secrets/mkos_pipeline_key" in dockerfile
    assert dockerfile.count("/run/secrets/") == 1


def test_builder_pipeline_swaps_the_stub_with_no_deps(dockerfile: str) -> None:
    stage = _stage(dockerfile, "builder-pipeline")
    assert len(stage) == 2
    run = stage[1]
    commands = [c.strip() for c in run.split("&&")]
    assert commands[0].endswith("uv pip uninstall mkos-pipeline-stub")
    assert "--mount=type=bind,from=pipeline-src,source=/pipeline" in commands[0]
    assert commands[1].split() == [
        "uv",
        "pip",
        "install",
        "--no-deps",
        "--no-cache",
        "--build-constraints",
        "/mnt/build-constraints.txt",
        "/mnt/mkos-pipeline",
    ]
    assert commands[2] == "rm -rf packages/pipeline-stub"
    check = commands[3]
    assert "from mkos_pipeline import run_listing_pipeline" in check
    assert "'mkos-pipeline' in names and 'mkos-pipeline-stub' not in names" in check


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
    must leave no category generatable), then the Amazon error map (SI9). No path arguments (the CLI's defaults ship in the image)
    and no shell variables (`set -eu`); only the last command execs."""
    assert _role_block(entrypoint, "migrate") == [
        "python -m app.cli db migrate",
        "python -m app.cli dictionary sync",
        "python -m app.cli rules load",
        "python -m app.cli templates load",
        "exec python -m app.cli errors load",
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
