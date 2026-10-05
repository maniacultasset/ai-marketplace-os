"""Supports AC11/AC15: every CI shell script parses and fails fast."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from tests.meta.test_compose import canon_local_bucket_names


def _scripts(repo_root: Path) -> list[Path]:
    scripts = sorted((repo_root / "scripts/ci").glob("*.sh"))
    assert scripts
    return scripts


def test_prove_gates_has_misspelled_bucket_leg(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    assert re.search(r"bucket marketplace-csv missing", text)
    bootstrap_legs = [m.start() for m in re.finditer(r"\bleg bootstrap", text)]
    assert len(bootstrap_legs) >= 2
    between = text[bootstrap_legs[0] : bootstrap_legs[1]]
    assert "down -v" in between
    assert "prove-gates-bootstrap" in between
    assert "cp .env.example .env" in text
    assert "seed_misspelled_bucket" in text


def test_prove_gates_has_s2_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    for name in (
        "contracts-broken-fixture",
        "contracts-pass-underscore",
        "contracts-missing-fixture",
        "contracts-no-fixtures",
        "schemas-drift-hand-edit",
        "schemas-drift-model-change",
        "lint-imports-schemas-sqla",
        "api-typecheck-schemas",
        "api-tests-schema-test",
    ):
        assert name in text


def test_shell_scripts_parse(repo_root: Path) -> None:
    for script in _scripts(repo_root):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (script.name, result.stderr)


def test_readme_compose_wait_is_scoped(repo_root: Path) -> None:
    """Bare `up -d --wait` fails once minio-init has exited 0 (Compose v5.5.1)."""
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    unscoped = [
        line
        for line in readme.splitlines()
        if "docker compose up -d --wait" in line and "postgres" not in line
    ]
    assert unscoped == []


def test_bootstrap_and_dev_scope_wait_to_long_running(repo_root: Path) -> None:
    """BD8: a one-shot minio-init makes bare `up -d --wait` exit 1 (Compose v5.5.1)."""
    bootstrap = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "up -d --wait postgres minio" in bootstrap
    assert "up -d minio-init" in bootstrap
    assert re.search(r"up -d --wait postgres minio", makefile)
    assert re.search(r"^dev:\n(?:\t.*\n)*\t.*up -d minio-init", makefile, re.MULTILINE)
    for text in (bootstrap, makefile):
        for line in text.splitlines():
            if "up -d --wait" in line:
                assert "postgres" in line and "minio" in line, line


def test_bootstrap_checks_minio_init_and_both_buckets(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "ps -a --format json minio-init" in text
    assert '"exited"' in text
    assert "ExitCode" in text
    match = re.search(r"buckets=\(([^)]+)\)", text)
    assert match is not None
    names = match.group(1).split()
    assert names == canon_local_bucket_names(repo_root)
    assert re.search(r"run --rm --no-deps minio-init ls", text)
    for forbidden in ("--entrypoint", "S3_SECRET_KEY", "S3_ACCESS_KEY", "MC_HOST"):
        assert not any(forbidden in line for line in text.splitlines()), forbidden
    up_at = text.index("up -d")
    uvicorn_at = text.index("uvicorn")
    init_at = text.index("minio-init")
    assert up_at < init_at < uvicorn_at


def test_shell_scripts_fail_fast(repo_root: Path) -> None:
    for script in _scripts(repo_root):
        lines = script.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "#!/usr/bin/env bash", script.name
        assert "set -euo pipefail" in lines[:15], script.name


_LOADER_KINDS = ("local", "export", "readonly")


def loader_problems(text: str) -> list[str]:
    problems: list[str] = []
    if re.search(r"""eval\s+["']\$\([^)]*exports""", text):
        problems.append("eval-subst")
    for kind in _LOADER_KINDS:
        if re.search(rf"""\b{kind}\s+[A-Za-z_][A-Za-z0-9_]*=["']\$\([^)]*exports""", text):
            problems.append(kind)
    return problems


def image_tag_problems(text: str) -> list[str]:
    problems: list[str] = []
    for match in re.finditer(r"check_image\.sh mkos-api:ci", text):
        nxt = text[match.end() : match.end() + 1]
        if nxt != "-":
            problems.append(match.group(0))
    return problems


def _without_comments(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ('eval "$(scripts/ci/worktree_env.sh exports)"', "eval-subst"),
        ('local e="$(scripts/ci/worktree_env.sh exports .worktree.env)"', "local"),
        ('export e="$(scripts/ci/worktree_env.sh exports .worktree.env)"', "export"),
        ('readonly e="$(scripts/ci/worktree_env.sh exports .worktree.env)"', "readonly"),
    ],
)
def test_loader_detector_flags_seeded_forms(text: str, kind: str) -> None:
    assert kind in loader_problems(text)


def test_loader_is_two_step(repo_root: Path) -> None:
    two_step = (
        'worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)"\n'
        'eval "$worktree_exports"\n'
    )
    assert loader_problems(two_step) == []
    for name in ("bootstrap.sh", "prove_gates.sh"):
        text = (repo_root / "scripts" / "ci" / name).read_text(encoding="utf-8")
        assert 'worktree_exports="$(scripts/ci/worktree_env.sh exports .worktree.env)"' in text
        assert 'eval "$worktree_exports"' in text
        assert loader_problems(text) == []


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_bootstrap_checks_before_compose(repo_root: Path) -> None:
    text = _code((repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8"))
    assert text.index("scripts/ci/worktree_env.sh check") < text.index("docker compose")


def test_bootstrap_api_port_is_one_variable(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert text.count('api_port="${API_HOST_PORT:-8000}"') == 1
    assert '--port "$api_port"' in text
    assert "localhost:" not in text
    for line in text.splitlines():
        if "curl " in line:
            assert "http://127.0.0.1:${api_port}" in line
    assert "8000" not in text.replace("${API_HOST_PORT:-8000}", "")


def test_bootstrap_port_probe_before_compose(repo_root: Path) -> None:
    text = _code((repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8"))
    probe = text.index('s.bind(("127.0.0.1"')
    assert "$api_port" in text
    assert "is already in use" in text
    assert probe < text.index("docker compose")


def test_bootstrap_resolved_config_loopback(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    config_at = text.index("docker compose config --format json")
    assert "127.0.0.1" in text[config_at:]
    assert 'image="mkos-api:ci-${project}"' in text
    assert config_at < text.index("up -d")


def test_bootstrap_image_tag_per_project(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert text.count('image="mkos-api:ci-${project}"') == 1
    assert text.count("docker build") == 1
    assert '-t "$image"' in text
    assert 'docker run --rm -e APP_ROLE=shell "$image"' in text
    assert 'scripts/ci/check_image.sh "$image"' in text
    assert "mkos-api:ci " not in text
    assert 'mkos-api:ci"' not in text
    assert image_tag_problems("scripts/ci/check_image.sh mkos-api:ci")


def test_check_image_and_workflow_contract(repo_root: Path) -> None:
    check = (repo_root / "scripts" / "ci" / "check_image.sh").read_text(encoding="utf-8")
    assert 'image="${1:?' in check
    workflow = (repo_root / ".github" / "workflows" / "ci-api.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("\n  bootstrap:\n") :]
    assert "make bootstrap" in job
    assert "COMPOSE_PROJECT_NAME" not in job
    assert "mkos-api" not in job


def test_bootstrap_detects_dead_server(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert 'kill -0 "$server"' in text
    assert "the API server exited" in text


def test_prove_gates_per_project(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    legs = list(re.finditer(r"(?m)^[ \t]*leg bootstrap.*$", text))
    assert len(legs) >= 2
    for match in legs:
        line = match.group(0)
        assert 'COMPOSE_PROJECT_NAME="prove-gates-bootstrap-${self_project}"' in line
        assert (
            "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:${PG_HOST_PORT:-5432}/"
            in line
        )
        assert "ALEMBIC_DATABASE_URL=" in line
        assert "${PG_HOST_PORT:-5432}" in line
        assert "S3_ENDPOINT_URL=http://localhost:${MINIO_API_HOST_PORT:-9000}" in line
        assert "PORT=${API_HOST_PORT:-8000}" in line
    for name in ("api-tests-failing-test", "tenant-isolation-no-marker"):
        leg = re.search(rf"(?m)^[ \t]*leg {name}.*$", text)
        assert leg is not None
        assert 'COMPOSE_PROJECT_NAME="$self_project"' in leg.group(0)
    assert 'cp "$root/.env" .env' in text
    for match in legs:
        following = text[match.end() : match.end() + 800]
        assert 'docker compose -p "prove-gates-bootstrap-${self_project}" down -v' in following
        assert 'docker image rm -f "mkos-api:ci-prove-gates-bootstrap-${self_project}"' in following
    body = _without_comments(text)
    for number, token in (
        ("5432", "${PG_HOST_PORT:-5432}"),
        ("8000", "${API_HOST_PORT:-8000}"),
        ("9000", "${MINIO_API_HOST_PORT:-9000}"),
    ):
        assert number not in body.replace(token, "")


def test_readme_documents_track_b(repo_root: Path) -> None:
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    keys = ("PG_HOST_PORT", "MINIO_API_HOST_PORT", "MINIO_CONSOLE_HOST_PORT", "API_HOST_PORT")
    assert ".worktree.env" in readme
    assert "git worktree add" in readme
    for key in keys:
        assert key in readme
    flat = re.sub(r"\s+", " ", readme)
    assert (
        "The default host ports are Postgres 5432, the API 8000, MinIO API 9000 and console 9001"
        in flat
    )
    assert '(e="$(scripts/ci/worktree_env.sh exports)" && eval "$e" && docker compose' in readme
    assert "docker compose down -v" in readme
    assert "set -a" not in readme
    for line in readme.splitlines():
        if ".env" in line and ".worktree.env" not in line and ".env.example" not in line:
            for key in keys:
                assert f"{key}=" not in line


def test_bootstrap_runs_alembic_image_migrate_and_mimic(repo_root: Path) -> None:
    """CANON-ADDENDUM.md §7: bootstrap-db, then Alembic, then the app; the image's `migrate` role
    runs on the compose network before the Cloud SQL mimic."""
    text = _code((repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8"))
    alembic = "uv run --frozen alembic -c apps/api/alembic.ini upgrade head"
    assert text.index("make bootstrap-db") < text.index(alembic) < text.index("uvicorn")
    migrate_at = text.index("APP_ROLE=migrate")
    assert text.index("scripts/ci/check_image.sh") < migrate_at < text.index("make bootstrap-mimic")
    run = text[text.rindex("docker run", 0, migrate_at) : text.index("\n", migrate_at)]
    assert '--network "$network"' in run
    assert "ALEMBIC_DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/" in run
    assert '"$image"' in run
    assert "networks" in text and '"default"' in text
