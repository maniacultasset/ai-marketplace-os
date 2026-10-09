"""Supports AC11/AC15: every CI shell script parses and fails fast."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
        # S10 C14: bootstrap refuses a superuser app login; the legs pass the role logins
        assert (
            "DATABASE_URL=postgresql+psycopg://app_rw:ci_only_app_rw_not_a_real_password@"
            "localhost:${PG_HOST_PORT:-5432}/" in line
        )
        assert (
            "ALEMBIC_DATABASE_URL=postgresql+psycopg://migrator:ci_only_migrator_not_a_real_password@"
            "localhost:${PG_HOST_PORT:-5432}/" in line
        )
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
    # S10 C14: as the migrator login from .env, rewritten onto the compose network
    assert '-e ALEMBIC_DATABASE_URL="$migrator_dsn"' in run
    assert '"$image"' in run
    assert "networks" in text and '"default"' in text


S5_LEGS = {
    "api-tests-alembic-drift": ("api-tests", "New upgrade operations detected"),
    "api-tests-roundtrip-broken": ("api-tests", "seeded downgrade failure"),
    "api-tests-squawk-missing": ("api-tests", "squawk did not check the SQL"),
    "api-lint-alembic-environ": ("api-lint", ": R1 "),
    "lint-imports-cli-imported": ("lint-imports", "nothing imports app.cli BROKEN"),
}


def test_prove_gates_has_s5_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    for name, (gate, regex) in S5_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
    seed = re.search(r"(?m)^seed_alembic_environ\(\) \{(.*)\}$", code)
    assert seed is not None
    assert "apps/api/alembic/env.py" in seed.group(1)
    assert "def _seeded()" in seed.group(1) and "import os" in seed.group(1)
    created = code.index("createdb -U postgres mos_prove")
    dropped = code.index("dropdb -U postgres --force mos_prove")
    seeded = ("api-tests-alembic-drift", "api-tests-roundtrip-broken", "api-tests-squawk-missing")
    api_legs = [code.index(f"leg {name} ") for name in seeded]
    assert created < min(api_legs) and max(api_legs) < dropped
    assert "0000_bootstrap.sql" in code[created : min(api_legs)]
    for name in seeded:
        line = re.search(rf"(?m)^[ \t]*leg {name}.*$", code)
        assert line is not None
        assert (
            "TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:"
            "${PG_HOST_PORT:-5432}/mos_prove" in line.group(0)
        )


S17A_LEGS = {
    "lint-imports-image-checks-sqla": (
        "lint-imports",
        "image checks are pure BROKEN",
        "seed_image_checks_sqla",
        "import sqlalchemy",
    ),
    "lint-imports-image-checks-repo": (
        "lint-imports",
        "image checks are pure BROKEN",
        "seed_image_checks_repo",
        "import app.repositories",
    ),
}


def test_prove_gates_has_s17a_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    for name, (gate, regex, seed_name, line) in S17A_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert leg.group(2) == seed_name, name
        seed = re.search(rf"(?m)^{seed_name}\(\) \{{(.*)\}}$", code)
        assert seed is not None, seed_name
        assert "apps/api/app/services/image_checks.py" in seed.group(1)
        assert f"'{line}'" in seed.group(1)


def test_check_image_probes_the_api_surface(repo_root: Path) -> None:
    # S3 T21: the built image serves the frozen surface and ships no local mock.
    text = (repo_root / "scripts" / "ci" / "check_image.sh").read_text(encoding="utf-8")
    assert "-o -path /src/apps/api/app_mock" in text
    assert '[ "$code" = "401" ]' in text
    # S10: an unverified token is refused (401) before any database; the auth leg proves the 501
    assert text.count('[ "$code" = "401" ]') == 2
    assert '[ "$code" = "501" ]' not in text
    assert "-H 'Authorization: Bearer x'" in text
    assert "x-request-id: " in text
    assert "from app.api.v1.surface import OPS; print(len(OPS))" in text
    assert '[ "$served" = "$registry" ]' in text
    assert "35" not in text  # the count comes from the image's registry, never a literal


def test_mock_smoke_script(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "mock_smoke.sh").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in text
    assert "Prefer: code=404" in text
    assert "/products/0b1c2d3e-" in text
    assert "kill" in text


S9_LEGS = {
    "lint-imports-platform-scope-worker": ("lint-imports", ": R5 "),
    "lint-imports-scope-ok-service": ("lint-imports", ": R6 "),
    "api-lint-text-in-service": ("api-lint", ": R4 "),
    "api-lint-text-attribute-chain": ("api-lint", ": R4 "),
    "api-lint-engine-in-worker": ("api-lint", ": R4 "),
    "tenant-isolation-no-filter": ("tenant-isolation", "test_list_returns_only_own_rows"),
    "tenant-isolation-no-guard": ("tenant-isolation", "test_raw_select_raises_guard"),
    "tenant-isolation-superuser": ("tenant-isolation", "test_isolation_runs_as_app_rw"),
    "tenant-isolation-guard-all-mappers": (
        "tenant-isolation",
        r"test_raw_select_raises_guard\[[a-z_]+-(join|subquery|exists|cte|select_from|core_table)\]",
    ),
    "tenant-isolation-guard-table-only": (
        "tenant-isolation",
        r"test_raw_select_raises_guard\[[a-z_]+-table_clause\]",
    ),
    "tenant-isolation-no-before-flush": ("tenant-isolation", "test_flush_rejects_other_tenant"),
    "tenant-isolation-no-marker": ("tenant-isolation", "deselected"),
}


def test_prove_gates_has_s9_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    for name, (gate, regex) in S9_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert re.search(rf"(?m)^{re.escape(leg.group(2))}\(\) \{{", code), leg.group(2)
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    for name, (gate, _) in S9_LEGS.items():
        if gate == "tenant-isolation":
            assert code.index(f"leg {name} ") > docker, name
    superuser = re.search(r"(?m)^[ \t]*leg tenant-isolation-superuser .*$", code)
    assert superuser is not None
    assert "ISO_DATABASE_URL=postgresql+psycopg://postgres:postgres@" in superuser.group(0)


def test_bootstrap_seeds_after_migrate(repo_root: Path) -> None:
    """S9 T21 (A§7): the host seed runs after Alembic and before the API starts; the image's
    seed role runs after its migrate role and before the Cloud SQL mimic."""
    raw = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "no seed until S9" not in raw
    text = _code(raw)
    alembic = text.index("uv run --frozen alembic -c apps/api/alembic.ini upgrade head")
    host_seed = text.index("uv run --frozen python -m app.cli seed --profile demo")
    assert alembic < host_seed < text.index("uvicorn app.main:app")
    image_migrate = text.index("APP_ROLE=migrate")
    image_seed = text.index("APP_ROLE=seed")
    assert image_migrate < image_seed < text.index("make bootstrap-mimic")
    run = text[text.rindex("docker run", 0, image_seed) : text.index("\n", image_seed)]
    assert '--network "$network"' in run and "APP_ENV=local" in run
    assert "SEED_PROFILE" not in run  # unset: the entrypoint's demo default
    # S10 C14: the seed writes tenant rows as app_rw and platform rows as migrator
    assert '-e DATABASE_URL="$app_dsn"' in run
    assert '-e ALEMBIC_DATABASE_URL="$migrator_dsn"' in run


def test_prove_gates_has_s14a_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    legs = {
        "api-lint-engine-openai": ("api-lint", "TID251", "seed_engine_openai"),
        "lint-imports-engine-sqla": (
            "lint-imports",
            "app.services.rules_engine is pure .*BROKEN",
            "seed_engine_sqla",
        ),
        "lint-imports-engine-repositories": (
            "lint-imports",
            "app.services.rules_engine is pure .*BROKEN",
            "seed_engine_repositories",
        ),
        "api-tests-engine-impure": (
            "api-tests",
            "test_rules_engine_import_allowlist",
            "seed_engine_impure",
        ),
        "api-tests-engine-coverage": (
            "api-tests",
            "rules_engine branch coverage",
            "seed_engine_coverage",
        ),
    }
    for name, (gate, regex, seed) in legs.items():
        match = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' \\?\s*(\S+)", text)
        assert match is not None, name
        assert match.group(1) == regex, name
        assert match.group(2) == seed, name
        assert re.search(rf"(?m)^{seed}\(\) \{{", text), seed
    docker = text.index('if [ "$with_docker" -eq 1 ]; then')
    assert text.index("leg api-tests-engine-impure") > docker
    assert text.index("leg api-tests-engine-coverage") > docker
    assert "apps/api/app/services/rules_engine/x.py" in text
    assert "import openai" in text


def test_bootstrap_has_s11_e2e_leg(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    code = _code(text)
    assert "-H 'X-Request-ID: s11-e2e'" in code
    assert "/api/v1/__s11_e2e" in code
    assert 'docker logs "$e2e_container"' in code
    assert 'docker rm -f "$e2e_container"' in code.split("trap cleanup EXIT")[0]
    assert "trap cleanup EXIT" in code
    run = code[code.index('docker run -d --name "$e2e_container"') :]
    run = run[: run.index('"$image"')]
    env_file = run.index("--env-file .env")
    assert run.index("-e LOG_FORMAT=json") > env_file
    assert run.index("-e APP_ENV=local") > env_file
    assert code.index('scripts/ci/check_image.sh "$image"') < code.index("X-Request-ID: s11-e2e")


def test_prove_gates_has_s11_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    docker = text[text.index('if [ "$with_docker" -eq 1 ]; then') :]
    for name, seed, expected in (
        ("api-tests-no-log-redaction", "seed_no_log_redaction", "test_redaction_logs"),
        (
            "api-tests-no-request-id-default",
            "seed_no_request_id_default",
            "test_request_id_stamped_on_rows|test_every_request_id_column_stamped",
        ),
    ):
        assert f"leg {name} api-tests '{expected}' {seed} " in docker
        assert f"{seed}() {{" in text
    assert "        _redact,\\n        _finalise," in text
    assert "default=current_request_id" in text


S10A_LEGS = {
    "api-tests-lv-guard-dropped": ("api-tests", "test_db_maker_checker_generator"),
    "api-tests-audit-update-granted": ("api-tests", "test_audit_append_only"),
}


def test_prove_gates_has_s10a_legs(repo_root: Path) -> None:
    """S10a: a dropped lv_guard and an UPDATE grant on audit_log each turn api-tests red, on a
    fresh throwaway mos_prove (never mos_test), inside the docker block."""
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    for name, (gate, regex) in S10A_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+) (.*)$", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert re.search(rf"(?m)^{re.escape(leg.group(2))}\(\) \{{", code), leg.group(2)
        assert "TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:" in leg.group(3)
        assert "/mos_prove make api-tests" in leg.group(3), name
        start = code.index(f"leg {name} ")
        assert start > docker, name
        before = code.rfind("fresh_prove_db", 0, start)
        assert before > docker and "leg " not in code[before:start].replace(
            f"leg {name} ", ""
        ), name
    seed = re.search(r"(?m)^seed_lv_guard_dropped\(\) \{(.*?)^\}", code, re.S)
    assert seed is not None
    assert "DROP TRIGGER lv_guard ON public.listing_versions" in seed.group(1)
    assert 'down_revision = "\'"$head"' in seed.group(1)
    grant = re.search(r"(?m)^seed_audit_update_granted\(\) \{(.*?)^\}", code, re.S)
    assert grant is not None
    assert "GRANT UPDATE ON public.audit_log TO app_rw;" in grant.group(1)
    assert "alembic/sql/0004/03_append_only.up.sql" in grant.group(1)


S10B_LEGS = {"tenant-isolation-policy-dropped": ("api-tests", "test_rls_forced_everywhere")}


def test_prove_gates_has_s10b_legs(repo_root: Path) -> None:
    """S10b: a seeded revision that drops listings' t_tenant policy turns api-tests red on the
    RLS catalog assertion, on a fresh throwaway mos_prove, inside the docker block."""
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    for name, (gate, regex) in S10B_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+) (.*)$", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert re.search(rf"(?m)^{re.escape(leg.group(2))}\(\) \{{", code), leg.group(2)
        assert "/mos_prove make api-tests" in leg.group(3), name
        start = code.index(f"leg {name} ")
        before = code.rfind("fresh_prove_db", 0, start)
        assert start > docker and before > docker, name
        assert "leg " not in code[before:start].replace(f"leg {name} ", ""), name
    seed = re.search(r"(?m)^seed_rls_policy_dropped\(\) \{(.*?)^\}", code, re.S)
    assert seed is not None
    assert "DROP POLICY t_tenant ON public.listings" in seed.group(1)
    assert 'down_revision = "\'"$head"' in seed.group(1)


# ---- S10 C14: the auth leg on the image, the app as app_rw (CODE-PLAN S10 Q5) ----


def _bootstrap_code(repo_root: Path) -> str:
    return _code((repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8"))


def test_bootstrap_asserts_app_rw(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    assert "postgres:postgres@" not in code
    assert 'app_dsn="$(env_url DATABASE_URL | on_network)"' in code
    assert 'migrator_dsn="$(env_url ALEMBIC_DATABASE_URL | on_network)"' in code
    assert "postgresql+psycopg://app_rw:*@postgres:5432/marketplace_os) ;;" in code
    assert "postgresql+psycopg://migrator:*@postgres:5432/marketplace_os) ;;" in code
    serve = code[code.index('docker run -d --name "$e2e_container"') :]
    assert '-e DATABASE_URL="$app_dsn"' in serve[: serve.index('"$image"')]
    check = code.index("SELECT current_user, r.rolsuper, r.rolbypassrls")
    assert '("app_rw", False, False)' in code[check : check + 400]
    # the role check runs over the app's own DSN, before any auth HTTP step
    assert code.rindex('-e DATABASE_URL="$app_dsn"', 0, check) > code.index("s11 e2e leg OK")
    assert check < code.index("scripts/ci/auth_e2e.py")


def _auth_steps(repo_root: Path) -> list[tuple[str, int]]:
    """`(label, wanted status)` of every `leg.expect(...)` and `leg.login(..., want=...)` call in
    the auth leg, in source order."""
    import ast

    tree = ast.parse((repo_root / "scripts" / "ci" / "auth_e2e.py").read_text(encoding="utf-8"))
    steps: list[tuple[int, int, str, int]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = node.func.attr
        if name == "expect" and len(node.args) >= 2:
            label, want = node.args[0], node.args[1]
            constants = isinstance(label, ast.Constant) and isinstance(want, ast.Constant)
            if constants and isinstance(label.value, str) and isinstance(want.value, int):  # type: ignore[attr-defined]
                steps.append((node.lineno, node.col_offset, label.value, want.value))  # type: ignore[attr-defined]
        if name == "login":
            wants = [k.value for k in node.keywords if k.arg == "want"]
            email = node.args[0] if node.args else None
            if wants and isinstance(wants[0], ast.Constant) and isinstance(email, ast.Constant):
                status, address = wants[0].value, email.value
                if isinstance(status, int) and isinstance(address, str):
                    steps.append((node.lineno, node.col_offset, f"login {address}", status))
    return [(label, want) for _, _, label, want in sorted(steps)]


def test_bootstrap_has_auth_leg(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    leg = code.index('python3 scripts/ci/auth_e2e.py "http://127.0.0.1:${api_port}"')
    rm = code.index('docker rm -f "$e2e_container" >/dev/null', leg)
    assert code.index("s11 e2e leg OK") < leg < rm < code.index("APP_ROLE=migrate")
    steps = _auth_steps(repo_root)
    for step in (
        ("me", 200),
        ("a verified token reaches a stub", 501),
        ("reviewer generate", 403),
        ("refresh reuse", 401),
        ("refresh after reuse", 401),
        ("refresh after logout", 401),
        ("switch to a tenant not mine", 404),
        ("patch another tenant's member", 404),
        ("demote the last owner", 409),
        ("login admin@bharat.test", 429),
    ):
        assert step in steps, step


def test_bootstrap_has_switch_and_members_happy_path(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    setup = code.index("SET SESSION AUTHORIZATION migrator;")
    assert "WHERE t.slug = 'bharat' AND lower(u.email) = 'operator@acme.test'" in code[setup:]
    assert "ON CONFLICT (tenant_id, user_id) DO NOTHING" in code[setup:]
    assert setup < code.index("scripts/ci/auth_e2e.py")
    steps = _auth_steps(repo_root)
    order = [
        ("switch to bharat", 200),
        ("me after switch", 200),
        ("me on the old token", 200),
        ("list members", 200),
        ("promote reviewer", 200),
        ("an operator patches a member", 403),
        ("demote back", 200),
        ("deactivate", 200),
        ("refresh after deactivation", 401),
        ("login reviewer@acme.test", 401),
        ("reactivate", 200),
    ]
    positions = [steps.index(step) for step in order]
    assert positions == sorted(positions)
    script = (repo_root / "scripts" / "ci" / "auth_e2e.py").read_text(encoding="utf-8")
    # HTTP only: the script opens no database connection and runs no subprocess
    body = script.split('"""', 2)[2]
    for banned in ("psycopg", "subprocess", "docker", "psql"):
        assert banned not in body, banned


S10C_LEGS = {
    "api-tests-route-without-role": ("seed_route_without_role", "test_every_route_declares_role"),
    "api-tests-limiter-off": ("seed_limiter_off", "test_rate_limit_sixth_login_429"),
}


def test_prove_gates_has_s10c_legs(repo_root: Path) -> None:
    """S10c (CODE-PLAN C15): a route with no policy and a login without its limiter each turn
    api-tests red on the test that guards them; the S3 case-matrix legs seed a route that is
    still a stub (S10 implemented auth_me)."""
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    for name, (seed, regex) in S10C_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} api-tests '([^']*)' (\S+) (.*)$", code)
        assert leg is not None, name
        assert (leg.group(1), leg.group(2)) == (regex, seed), name
        assert 'COMPOSE_PROJECT_NAME="$self_project" make api-tests' in leg.group(3)
        assert code.index(f"leg {name} ") > docker
        assert re.search(rf"(?m)^{seed}\(\) \{{", code), seed
    route = re.search(r"(?m)^seed_route_without_role\(\) \{(.*?)^\}", code, re.S)
    assert route is not None and "dependencies=[_policy(op)]" in route.group(1)
    limiter = re.search(r"(?m)^seed_limiter_off\(\) \{(.*?)^\}", code, re.S)
    assert limiter is not None and "enforce_login_limit(request, body.email)" in limiter.group(1)
    assert "tenants_me: missing endpoint cases" in code
    assert "tenants_me: presence-only bearer_required" in code
    assert "auth_me: " not in code


def _env_example_value(repo_root: Path, key: str) -> str:
    text = (repo_root / ".env.example").read_text(encoding="utf-8")
    found = re.search(rf"(?m)^{key}=(.*)$", text)
    assert found is not None, key
    return found.group(1)


def test_ci_bootstrap_job_env_uses_app_roles(
    repo_root: Path, load_yaml: Callable[[Path], Any]
) -> None:
    """S10c round-1 H1: the process env wins over `.env` in bootstrap.sh's env_url(), so the
    bootstrap job's own env must log in as app_rw (DATABASE_URL) and migrator
    (ALEMBIC_DATABASE_URL), the same DSNs `.env.example` gives the local app."""
    workflow = load_yaml(repo_root / ".github" / "workflows" / "ci-api.yml")
    env = workflow["jobs"]["bootstrap"].get("env") or {}
    if "DATABASE_URL" in env:
        assert env["DATABASE_URL"].startswith("postgresql+psycopg://app_rw:")
        assert env["DATABASE_URL"] == _env_example_value(repo_root, "DATABASE_URL")
    if "ALEMBIC_DATABASE_URL" in env:
        assert env["ALEMBIC_DATABASE_URL"].startswith("postgresql+psycopg://migrator:")
        assert env["ALEMBIC_DATABASE_URL"] == _env_example_value(repo_root, "ALEMBIC_DATABASE_URL")


# S12 T13 (AC6, Done-when 2): a products router importing sqlalchemy or app.db fails lint-imports.
S12_ROUTER_LEGS = {
    "lint-imports-products-sqla": (
        "seed_products_router_sqlalchemy",
        "from sqlalchemy import select",
    ),
    "lint-imports-products-db": ("seed_products_router_db", "import app.db.session"),
}


def test_prove_gates_has_products_router_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (seed_name, line) in S12_ROUTER_LEGS.items():
        leg = re.search(
            rf"(?m)^[ \t]*leg {re.escape(name)} lint-imports '([^']*)' \\?\s*(\S+)", code
        )
        assert leg is not None, name
        assert leg.group(1) == "app.api never imports sqlalchemy or app.db directly BROKEN", name
        assert leg.group(2) == seed_name, name
        seed = re.search(rf"(?m)^{seed_name}\(\) \{{(.*)\}}$", code)
        assert seed is not None, seed_name
        assert "append apps/api/app/api/v1/routers/products.py" in seed.group(1)
        assert f"'{line}'" in seed.group(1)


# ---- S12 T14 (AC8; PLAN D8): the walking-skeleton e2e and the shared limiter reset ----

E2E_STEPS = (
    "health",
    "login_acme_operator",
    "create_201",
    "get_200",
    "list_contains",
    "create_again_409",
    "login_acme_reviewer",
    "reviewer_create_403",
    "no_token_401",
    "login_bharat_operator",
    "bharat_get_404",
    "bharat_list_lacks",
    "db_row_under_acme",
)
NON_LOCAL_URLS = (
    "https://example.invalid",
    "http://example.invalid",
    "http://localhost.example.invalid",
    "http://127.0.0.1@example.invalid",
    "http://user:pw@127.0.0.1:8000",
    "https://127.0.0.1:8000",
    "http://127.0.0.1:8000/api",
)


def _stub_path(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """PATH with `docker` and `curl` stubs that only record their argv (one line per call)."""
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("docker", "curl"):
        stub = bin_dir / name
        stub.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "{name} $*" >> "{log}"\n', encoding="utf-8"
        )
        stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return env, log


def _e2e_script(repo_root: Path) -> str:
    return (repo_root / "scripts" / "e2e" / "walking_skeleton.sh").read_text(encoding="utf-8")


def test_e2e_script_steps(repo_root: Path) -> None:
    text = _e2e_script(repo_root)
    code = _code(text)
    assert "set -euo pipefail" in code
    steps = re.findall(r"(?m)^step (\w+)$", code)
    assert tuple(steps) == E2E_STEPS
    assert 'sku="E2E-$(date +%s)-$RANDOM"' in code
    # the port comes from .worktree.env / API_HOST_PORT, never a literal
    assert not re.search(r":\d{2,5}\b", code)
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    # a failure names the step and the status, never a body
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_e2e_refuses_non_local_url(repo_root: Path, tmp_path: Path, url: str) -> None:
    env, log = _stub_path(tmp_path)
    result = subprocess.run(
        [str(repo_root / "scripts" / "e2e" / "walking_skeleton.sh"), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_reset_rate_limits_refuses_non_local_url(repo_root: Path, tmp_path: Path, url: str) -> None:
    env, log = _stub_path(tmp_path)
    result = subprocess.run(
        [str(repo_root / "scripts" / "ci" / "reset_rate_limits.sh"), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("url", ["http://127.0.0.1:8123", "http://localhost:8123/"])
def test_reset_rate_limits_truncates_only_counters(
    repo_root: Path, tmp_path: Path, url: str
) -> None:
    env, log = _stub_path(tmp_path)
    result = subprocess.run(
        [str(repo_root / "scripts" / "ci" / "reset_rate_limits.sh"), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "docker compose exec -T postgres psql -U postgres -d marketplace_os -c "
        "TRUNCATE rate_limit_counters"
    ]


def test_e2e_scripts_never_truncate(repo_root: Path) -> None:
    scripts = sorted((repo_root / "scripts" / "e2e").glob("*.sh"))
    assert scripts
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "TRUNCATE" not in text.upper(), script
        assert "rate_limit_counters" not in text, script
        assert "RATE_LIMIT_" not in text, script


def test_local_url_check(script: Callable[[str], Any]) -> None:
    local_url = script("scripts/ci/local_url.py")
    for url in (*NON_LOCAL_URLS, "", "http://", "http://127.0.0.1:0", "http://[::1]:8000"):
        assert not local_url.is_local(url), url
    for url in ("http://127.0.0.1", "http://127.0.0.1:8080", "http://localhost:8000/"):
        assert local_url.is_local(url), url


# ---- S12 T15 (AC7, AC8; Done-when 3): bootstrap runs the e2e twice on the built image ----

E2E_CALL = 'scripts/e2e/walking_skeleton.sh "http://127.0.0.1:${api_port}"'
RESET_CALL = 'scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"'


def test_bootstrap_runs_e2e(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    assert code.count(E2E_CALL) == 2
    first = code.index(E2E_CALL)
    serve = code.index('docker run -d --name "$e2e_container"')
    auth = code.index('python3 scripts/ci/auth_e2e.py "http://127.0.0.1:${api_port}"')
    ready = code.index('/readyz")"', auth)
    rm = code.index('docker rm -f "$e2e_container" >/dev/null', auth)
    assert serve < auth < ready < first < code.rindex(E2E_CALL) < rm
    assert '[ "$ready" = "200" ]' in code[ready:first]
    # the trap that removes the container is set before the image serves
    assert code.index("trap cleanup EXIT") < serve


def test_bootstrap_resets_before_each_e2e_run(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    e2e = [i for i, line in enumerate(lines) if line == E2E_CALL]
    assert len(e2e) == 2
    for i in e2e:
        assert lines[i - 1] == RESET_CALL
    assert "TRUNCATE" not in code.upper()
    assert "rate_limit_counters" not in code


def test_auth_leg_stub_probe_is_still_a_stub(repo_root: Path) -> None:
    # S12 implemented GET /products: the auth leg's "verified token reaches a stub" probe moved
    # to a GET route that is still S3's 501 stub.
    import importlib

    from app.api.v1.surface import OPS, is_stub

    script = (repo_root / "scripts" / "ci" / "auth_e2e.py").read_text(encoding="utf-8")
    probe = re.search(
        r'"a verified token reaches a stub",\s*501,\s*leg\.call\("GET", "([^"]+)"', script
    )
    assert probe is not None
    path = probe.group(1).split("?", 1)[0]
    ops = [o for o in OPS.values() if o.method == "GET" and o.path == path]
    assert len(ops) == 1
    module = importlib.import_module(f"app.api.v1.routers.{ops[0].group}")
    assert is_stub(getattr(module, ops[0].op_id))


# ---- S13 A8: a local worker beside `make dev` ----


def test_make_worker_target(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert ".PHONY: worker\nworker:\n" in makefile
    block = makefile.split("\nworker:\n", 1)[1].split("\n\n", 1)[0]
    assert "APP_ROLE=worker" in block
    assert "APP_NAME=marketplace-os-worker" in block
    assert "OTEL_SERVICE_NAME=marketplace-os-worker" in block
    assert "uv run --frozen python -m app.workers.main" in block
    assert "uvicorn" not in block and "--port" not in block


# ---- S13 A16: the bootstrap check boots the image as a worker ----


def _worker_leg(repo_root: Path) -> str:
    text = (repo_root / "scripts/ci/bootstrap.sh").read_text(encoding="utf-8")
    return text.split("# S13 worker leg", 1)[1].split("# The Cloud Run migrate job", 1)[0]


def test_bootstrap_has_worker_leg(repo_root: Path) -> None:
    leg = _worker_leg(repo_root)
    assert "-e APP_ROLE=worker" in leg
    assert '-e DATABASE_URL="$app_dsn"' in leg  # app_rw, on the compose network
    assert '--network "$network"' in leg
    assert "procrastinate_workers" in leg
    assert "seq 1 30" in leg  # registered within 30 s
    text = (repo_root / "scripts/ci/bootstrap.sh").read_text(encoding="utf-8")
    assert '"$worker_container"' in text.split("cleanup() {", 1)[1].split("}", 1)[0]


def test_bootstrap_worker_leg_checks_exit_and_time(repo_root: Path) -> None:
    leg = _worker_leg(repo_root)
    assert "docker stop --time 10" in leg  # SIGTERM first, SIGKILL only after 10 s
    assert '"$worker_code" != "0"' in leg
    assert '"$worker_took" -ge 10' in leg
    assert "carries a DSN" in leg and "grep -Eq" in leg
    assert '"$(worker_rows)" != "0"' in leg  # unregistered on the way out


def test_prove_gates_has_s13a_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    assert "leg lint-imports-platform-scope-in-wrapper lint-imports ': R5 '" in text
    assert "leg lint-imports-job-guc-outside-context lint-imports ': R8 '" in text
    docker = text.split('if [ "$with_docker" -eq 1 ]; then', 1)[1]
    assert "leg api-tests-cas-without-status api-tests 'test_claim_cas_drops_duplicate'" in docker
    for seed in (
        "seed_platform_scope_in_wrapper",
        "seed_job_guc_outside_context",
        "seed_cas_without_status",
    ):
        assert f"{seed}() {{" in text


# ---- S15 T19 (AC16): the marketplace connect e2e ----

MARKETPLACE_STEPS = (
    "health",
    "fake_health",
    "login_acme_owner",
    "cleanup_live_amazon",
    "connect_200",
    "consent",
    "callback_303",
    "callback_replay_409",
    "login_bharat_owner",
    "bharat_confirm_409",
    "confirm_200",
    "list_active",
    "login_acme_reviewer",
    "reviewer_connect_403",
    "login_acme_operator",
    "operator_connect_403",
    "db_ciphertext_no_canary",
    "force_invalid_grant",
    "refresh_once",
    "list_needs_reconnect",
    "db_one_reconnect_task",
    "reconnect",
    "revoke_200",
    "revoke_again_200",
    "db_revoked_shredded",
)
MARKETPLACE_E2E = "scripts/e2e/marketplace_connect.sh"


def _marketplace_code(repo_root: Path) -> str:
    return _code((repo_root / MARKETPLACE_E2E).read_text(encoding="utf-8"))


def test_e2e_marketplace_connect_steps(repo_root: Path) -> None:
    code = _marketplace_code(repo_root)
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == MARKETPLACE_STEPS
    logins = re.findall(r"login (\w+@\w+\.test)", code)
    assert sorted(logins) == sorted(set(logins))  # one login per email per run
    assert not re.search(r":\d{2,5}\b", code)  # ports come from .worktree.env, never a literal
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    # the seller id passes the API's pattern (letters and digits only)
    assert 'seller="E2E$(date +%s)$RANDOM"' in code
    assert "--max-time" in code and "bound=60" in code


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_e2e_marketplace_connect_refuses_non_local_url(
    repo_root: Path, tmp_path: Path, url: str
) -> None:
    env, log = _stub_path(tmp_path)
    env["REFRESH_ONCE"] = "true"
    env["FAKE_URL"] = "http://127.0.0.1:4020"
    result = subprocess.run(
        [str(repo_root / MARKETPLACE_E2E), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""
    env["FAKE_URL"] = url
    fake = subprocess.run(
        [str(repo_root / MARKETPLACE_E2E), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert fake.returncode == 2, fake.stderr


def test_e2e_marketplace_connect_without_hooks_exits_2(repo_root: Path, tmp_path: Path) -> None:
    env, log = _stub_path(tmp_path)
    env.pop("REFRESH_ONCE", None)
    result = subprocess.run(
        [str(repo_root / MARKETPLACE_E2E), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "usage: FAKE_URL=" in result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_e2e_marketplace_connect_ends_revoked(repo_root: Path) -> None:
    """Later scripts connect their own account: this one leaves no live acme Amazon account."""
    code = _marketplace_code(repo_root)
    steps = list(re.findall(r"(?m)^step (\w+)$", code))
    account_steps = [
        s for s in steps if s.startswith(("connect", "confirm", "reconnect", "revoke"))
    ]
    assert account_steps[-2:] == ["revoke_200", "revoke_again_200"]
    after = code[code.index("step revoke_200") :]
    assert "connect_confirm" not in after and "/marketplaces/connect" not in after


S15_LEGS = {
    "lint-imports-unseal-from-router": (
        "lint-imports",
        "only app.adapters and app.workers import the decrypt path BROKEN",
    ),
    "tenant-isolation-hash-branch-pending": ("tenant-isolation", "hides_pending_row"),
    "api-tests-revoke-no-shred": ("api-tests", "ck_ma_revoked_shredded|test_revoke_crypto_shreds"),
}


def test_prove_gates_s15_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (gate, regex) in S15_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
    assert "import app.adapters.credentials" in code
    assert "AND token_enc IS NULL" in code and "0006_oauth_pending.py" in code
    assert "SET status = 'revoked', token_enc = NULL" in code
    # the migration leg re-migrates mos_iso afterwards with the checkout's own revisions
    hash_leg = code.index("leg tenant-isolation-hash-branch-pending")
    assert code.index("remigrate_iso >>", hash_leg) > hash_leg
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    assert code.index("leg api-tests-revoke-no-shred") > docker
    assert hash_leg > docker
    assert code.index("leg lint-imports-unseal-from-router") < docker


# ---- S20 T20 (AC5): the adapter import contracts and the preview case matrix are proven ----

S20_LEGS = {
    "lint-imports-adapter-from-service": (
        "lint-imports",
        "only app.adapters and app.workers reach the marketplace adapter and client BROKEN",
    ),
    "lint-imports-mapping-httpx": (
        "lint-imports",
        "adapter mapping, endpoint check, error map and registry do no I/O BROKEN",
    ),
    "api-tests-preview-case-matrix": ("api-tests", "listings_preview: missing endpoint cases"),
}


def test_prove_gates_s20_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (gate, regex) in S20_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
    assert "import app.adapters.amazon.adapter" in code
    assert "app/adapters/amazon/mapping.py" in code and "import httpx" in code
    assert 'case="conflict"' in code and "test_listings_preview.py" in code
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    assert code.index("leg lint-imports-adapter-from-service") < docker
    assert code.index("leg lint-imports-mapping-httpx") < docker
    assert code.index("leg api-tests-preview-case-matrix") > docker


# ---- S21 T24 (AC11, AC13): the reader-only sync contract and the publish case matrix ----

S21_LEGS = {
    "lint-imports-sync-imports-adapter": (
        "lint-imports",
        "the status sync reaches the marketplace only through the reader view BROKEN",
    ),
    "api-tests-publish-case-matrix": ("api-tests", "listings_publish: missing endpoint cases"),
}


def test_prove_gates_s21_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (gate, regex) in S21_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
    assert "apps/api/app/workers/status_sync.py" in code and "import app.workers.adapters" in code
    assert "test_listings_publish_api.py" in code
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    assert code.index("leg lint-imports-sync-imports-adapter") < docker
    assert code.index("leg api-tests-publish-case-matrix") > docker


def test_bootstrap_imports_pipeline(repo_root: Path) -> None:
    # S14b T14 (ADR-0013 §4): the image ships the pipeline module the generation worker imports.
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert '-c "import app, mkos_schemas, mkos_pipeline"' in text


# ---- S14b T15 (AC 16): the rules e2e ----

RULES_E2E_STEPS = (
    "health",
    "login_acme_owner",
    "rules_list_200",
    "dry_run_221_fails",
    "dry_run_200_passes",
    "no_token_401",
    "login_acme_reviewer",
    "reviewer_dry_run_403",
    "unknown_pair_422",
)


def test_rules_e2e_steps(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "e2e" / "rules.sh").read_text(encoding="utf-8")
    code = _code(text)
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == RULES_E2E_STEPS
    assert code.count("$(login ") == 2  # the login budget (S12)
    assert not re.search(r":\d{2,5}\b", code)
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert "psql" not in code and "docker compose exec" not in code  # HTTP only


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_rules_e2e_refuses_non_local_url(repo_root: Path, tmp_path: Path, url: str) -> None:
    env, log = _stub_path(tmp_path)
    result = subprocess.run(
        [str(repo_root / "scripts" / "e2e" / "rules.sh"), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


# ---- S14b T23 (AC 16): the admin e2e ----

ADMIN_E2E_STEPS = (
    "health",
    "admin_index_200",
    "listing_versions_list_200",
    "seeded_version_detail_has_title",
    "seeded_run_detail_has_field_too_long",
    "db_read_seeded_secrets_then_grep",
    "post_refused_405",
    "ops_queue_200",
)


def _admin_e2e(repo_root: Path) -> str:
    return (repo_root / "scripts" / "e2e" / "admin.sh").read_text(encoding="utf-8")


def test_admin_e2e_steps(repo_root: Path) -> None:
    from app.db.seed_ops import SEED_NAMESPACE

    code = _code(_admin_e2e(repo_root))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == ADMIN_E2E_STEPS
    assert not re.search(r":\d{2,5}\b", code)
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert f'seed_namespace="{SEED_NAMESPACE}"' in code  # the seed's fixed ids


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_admin_e2e_refuses_non_local_url(repo_root: Path, tmp_path: Path, url: str) -> None:
    env, log = _stub_path(tmp_path)
    result = subprocess.run(
        [str(repo_root / "scripts" / "e2e" / "admin.sh"), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_admin_e2e_psql_only_in_db_steps(repo_root: Path) -> None:
    current = "setup"
    hits: list[tuple[str, str]] = []
    for line in _code(_admin_e2e(repo_root)).splitlines():
        match = re.match(r"^step (\w+)$", line)
        if match:
            current = match.group(1)
        if "psql" in line or "docker compose exec" in line:
            hits.append((current, line.strip()))
    assert hits, "the db step reads the seeded secrets"
    assert all(step.startswith("db_") for step, _ in hits), hits


# ---- S14b T25 (AC 16; Done-when 7): ONBOARDING.md on a clean clone ----

ONBOARDING_E2E = "scripts/e2e/onboarding.sh"
DEFAULT_PORTS = {"5432", "9000", "9001", "8000", "4010"}


def _onboarding_e2e(repo_root: Path) -> str:
    return _code((repo_root / ONBOARDING_E2E).read_text(encoding="utf-8"))


def test_onboarding_e2e_uses_temp_clone_and_trap(repo_root: Path) -> None:
    code = _onboarding_e2e(repo_root)
    assert 'work="$(mktemp -d)"' in code
    assert 'git clone --quiet --local --branch "$branch" "$root" "$clone_dir"' in code
    assert "trap cleanup EXIT" in code
    assert "docker compose down -v --remove-orphans" in code
    assert 'rm -rf "$work"' in code
    assert 'kill -TERM -- "-$pid"' in code  # the background process group


def test_onboarding_e2e_own_project_name(repo_root: Path) -> None:
    assert 'project="mkos-onb-$$-$RANDOM"' in _onboarding_e2e(repo_root)


def test_e2e_target_skips_onboarding(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("\ne2e:\n", 1)[1].split("\n\n", 1)[0]
    assert '[ "$$(basename "$$script")" != onboarding.sh ] || continue' in recipe
    assert "\ne2e-onboarding:\n\tscripts/e2e/onboarding.sh\n" in makefile


def test_onboarding_e2e_shares_parser_with_test(repo_root: Path) -> None:
    code = _onboarding_e2e(repo_root)
    assert 'python3 "$root/scripts/ci/onboarding_commands.py" "$dir/ONBOARDING.md"' in code
    test = (repo_root / "apps/api/tests/meta/test_onboarding.py").read_text(encoding="utf-8")
    assert 'PARSER = "scripts/ci/onboarding_commands.py"' in test


def test_free_ports_helper_shared(repo_root: Path) -> None:
    code = _onboarding_e2e(repo_root)
    assert ". scripts/e2e/lib/free_ports.sh" in code
    assert "socket" not in code and "free_ports()" not in code
    assert "free_ports()" in (repo_root / "scripts/e2e/lib/free_ports.sh").read_text("utf-8")


def _stand_in(repo_root: Path, tmp_path: Path) -> Path:
    """A clone stand-in: a Makefile WORKTREE_KEYS line with one extra key, the real
    .env.example and worktree_env.sh, and its own git repository."""
    import shutil

    clone = tmp_path / "clone"
    (clone / "scripts/ci").mkdir(parents=True)
    shutil.copy(repo_root / "scripts/ci/worktree_env.sh", clone / "scripts/ci/worktree_env.sh")
    shutil.copy(repo_root / ".env.example", clone / ".env.example")
    (clone / "Makefile").write_text(
        "WORKTREE_KEYS := COMPOSE_PROJECT_NAME PG_HOST_PORT MINIO_API_HOST_PORT "
        "MINIO_CONSOLE_HOST_PORT API_HOST_PORT MOCK_HOST_PORT\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    return clone


def _prepare(repo_root: Path, clone: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo_root / ONBOARDING_E2E), "--prepare", str(clone)],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=60,
        check=False,
    )


def test_onboarding_e2e_allocates_ports_not_defaults(repo_root: Path, tmp_path: Path) -> None:
    clone = _stand_in(repo_root, tmp_path)
    result = _prepare(repo_root, clone)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in (clone / ".worktree.env").read_text().splitlines())
    ports = {k: v for k, v in values.items() if k != "COMPOSE_PROJECT_NAME"}
    assert set(ports) == {
        "PG_HOST_PORT",
        "MINIO_API_HOST_PORT",
        "MINIO_CONSOLE_HOST_PORT",
        "API_HOST_PORT",
        "MOCK_HOST_PORT",
    }
    assert len(set(ports.values())) == len(ports)
    assert not set(ports.values()) & DEFAULT_PORTS
    mine = subprocess.run(
        [str(repo_root / "scripts/ci/worktree_env.sh"), "exports", ".worktree.env"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for key, port in ports.items():
        assert f"export {key}={port}" not in mine
    assert values["COMPOSE_PROJECT_NAME"].startswith("mkos-onb-")
    env = (clone / ".env").read_text(encoding="utf-8")
    assert f"@localhost:{ports['PG_HOST_PORT']}/marketplace_os" in env
    assert f"S3_ENDPOINT_URL=http://localhost:{ports['MINIO_API_HOST_PORT']}" in env
    assert f"PORT={ports['API_HOST_PORT']}\n" in env
    leaked = {"DATABASE_URL", "ALEMBIC_DATABASE_URL", "S3_ENDPOINT_URL", "PORT", *ports}
    clean = {k: v for k, v in os.environ.items() if k not in leaked | {"COMPOSE_PROJECT_NAME"}}
    check = subprocess.run(
        ["scripts/ci/worktree_env.sh", "check"],
        cwd=clone,
        capture_output=True,
        text=True,
        env=clean,
    )
    assert check.returncode == 0, check.stderr
    forced = _prepare(
        repo_root,
        _stand_in(repo_root, tmp_path / "again"),
        ONBOARDING_FORCE_PORTS="PG_HOST_PORT=5432",
    )
    assert forced.returncode == 2, forced.stderr


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_onboarding_e2e_background_waits_and_is_killed(
    repo_root: Path, tmp_path: Path, outcome: str
) -> None:
    # sockets in a child process: pytest-socket blocks them in this one
    free = "import socket; s = socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])"
    port = int(subprocess.run(["python3", "-c", free], capture_output=True, text=True).stdout)
    served = tmp_path / "served"
    served.mkdir()
    (served / "healthz").write_text("ok", encoding="utf-8")
    clone = tmp_path / "clone"
    (clone / "scripts/ci").mkdir(parents=True)
    import shutil

    shutil.copy(repo_root / "scripts/ci/worktree_env.sh", clone / "scripts/ci/worktree_env.sh")
    (clone / ".worktree.env").write_text(f"API_HOST_PORT={port}\n", encoding="utf-8")
    marker = tmp_path / "ran"
    last = "false" if outcome == "failure" else f"touch {marker}"
    (clone / "ONBOARDING.md").write_text(
        "```sh\n"
        f"python3 -m http.server {port} --bind 127.0.0.1 --directory {served}"
        "  # background: wait /healthz\n"
        "make bootstrap  # skip: CI only\n"
        f"{last}\n"
        "```\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(repo_root / ONBOARDING_E2E), "--run", str(clone)],
        capture_output=True,
        text=True,
        env={**os.environ, "ONBOARDING_WAIT_SECONDS": "30"},
        timeout=90,
        check=False,
    )
    assert "SKIP make bootstrap: CI only" in result.stdout
    if outcome == "success":
        assert result.returncode == 0, result.stderr
        assert marker.exists()
    else:
        assert result.returncode != 0
    # nothing listens on the port any more: the background server was killed
    refused = (
        "import socket, sys\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
        "except ConnectionRefusedError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    assert subprocess.run(["python3", "-c", refused], capture_output=True).returncode == 0
    leftover = subprocess.run(
        ["pgrep", "-f", f"http.server {port}"], capture_output=True, text=True, check=False
    )
    assert leftover.stdout.strip() == "" or not any(_alive(int(p)) for p in leftover.stdout.split())


# ---- S13b B15 (AC 22): the HTTP import flow, its hooks, make e2e and the bootstrap leg ----

IMPORT_FLOW = "scripts/e2e/import_flow.sh"
IMPORT_STEPS = (
    "health",
    "login_acme_operator",
    "worker_start",
    "upload_202",
    "job_partial",
    "report_45_5",
    "failed_rows_5",
    "replay_same_ids",
    "key_reuse_409",
    "bharat_import_404",
    "templates_load",
    "retry_202",
    "retry_partial_46_4",
    "worker_stop",
    "upload_queued",
    "cancel_200",
)

# A `curl` stand-in for the import flow: it answers each request the flow makes from the URL,
# method and headers, writes the body to `-o` and prints the status (`-w '%{http_code}'`). The
# late job's status (`queued`, or `running` when a foreign worker took it) comes from LATE_STATUS.
_CURL_STUB = r"""#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
out = None; method = "GET"; headers = {}; form = ""; url = ""; data = ""
i = 0
while i < len(args):
    a = args[i]
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-H":
        k, _, v = args[i + 1].partition(": "); headers[k.lower()] = v; i += 2; continue
    if a == "-F": form = args[i + 1]; i += 2; continue
    if a == "--data-binary": data = args[i + 1]; i += 2; continue
    if a == "-w": i += 2; continue
    if a.startswith("http"): url = a
    i += 1
state = os.environ["STUB_STATE"]
with open(state, "a", encoding="utf-8") as fh:
    fh.write(f"{method} {url}\n")
retried = os.path.exists(state + ".retried")
token = headers.get("authorization", "")
path = re.sub(r"^https?://[^/]+", "", url)
status, body = 404, {"error": {"type": "NOT_FOUND"}}
if path == "/readyz":
    status, body = 200, {}
elif path.endswith("/auth/login"):
    who = json.loads(data)["email"]
    status, body = 200, {"access_token": "bharat" if "bharat" in who else "acme"}
elif path.endswith("/products/bulk"):
    key = headers.get("idempotency-key", "")
    if key.startswith("e2e-late"):
        status, body = 202, {"import_id": "I2", "job_id": "LATE"}
    elif "other.csv" in form:
        status, body = 409, {"error": {"type": "IDEMPOTENCY_KEY_REUSED"}}
    else:
        status, body = 202, {"import_id": "IMP", "job_id": "JOB"}
elif path.endswith("/jobs/JOB") or path.endswith("/jobs/RETRY"):
    status, body = 200, {"status": "partial"}
elif path.endswith("/jobs/LATE"):
    status, body = 200, {"status": os.environ.get("LATE_STATUS", "queued")}
elif path.endswith("/jobs/LATE/cancel"):
    status, body = 200, {"status": "cancelled"}
elif path.endswith("/imports/IMP/rows/retry"):
    open(state + ".retried", "w").close()
    status, body = 202, {"job_id": "RETRY"}
elif "/imports/IMP/rows" in path:
    status, body = 200, {"data": [{"row_number": n, "errors": [{"x": 1}]} for n in (8, 15, 22, 35, 43)]}
elif path.endswith("/imports/IMP"):
    if "bharat" in token:
        status, body = 404, {"error": {"type": "NOT_FOUND"}}
    else:
        counts = (46, 4) if retried else (45, 5)
        status, body = 200, {"total_rows": 50, "imported": counts[0], "failed": counts[1], "rows": []}
if out:
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
sys.stdout.write(str(status))
"""


def _run_import_flow(
    repo_root: Path, tmp_path: Path, *, hooks: bool = True, late: str = "queued"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_CURL_STUB, encoding="utf-8")
    curl.chmod(0o755)
    hook_log = tmp_path / "hooks.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_STATE"] = str(tmp_path / "requests.log")
    env["LATE_STATUS"] = late
    for name in ("WORKER_START", "WORKER_STOP", "TEMPLATES_LOAD"):
        env.pop(name, None)
    if hooks:
        env["WORKER_START"] = f'echo start >> "{hook_log}"'
        env["WORKER_STOP"] = f'echo stop >> "{hook_log}"'
        env["TEMPLATES_LOAD"] = f'test -s "$1" && echo templates >> "{hook_log}"'
    result = subprocess.run(
        [str(repo_root / IMPORT_FLOW), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    calls = hook_log.read_text(encoding="utf-8").split() if hook_log.exists() else []
    return result, calls


def test_e2e_import_flow_steps(repo_root: Path, tmp_path: Path) -> None:
    code = _code((repo_root / IMPORT_FLOW).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == IMPORT_STEPS
    assert "POLL_LIMIT_S=120" in code and "$(( $(date +%s) + POLL_LIMIT_S ))" in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert not re.search(r":\d{2,5}\b", code)
    result, calls = _run_import_flow(repo_root, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == ["start", "templates", "stop"]  # in this order, once each
    assert result.stdout.strip().endswith("e2e: import_flow OK (import IMP)")


def test_e2e_import_flow_without_hooks_exits_2(repo_root: Path, tmp_path: Path) -> None:
    result, calls = _run_import_flow(repo_root, tmp_path, hooks=False)
    assert result.returncode == 2
    assert result.stderr.startswith(
        "usage: WORKER_START=<cmd> WORKER_STOP=<cmd> TEMPLATES_LOAD=<cmd>"
    )
    assert not (tmp_path / "requests.log").exists()
    assert calls == []


def test_e2e_import_flow_fails_fast_if_foreign_worker(repo_root: Path, tmp_path: Path) -> None:
    result, calls = _run_import_flow(repo_root, tmp_path, late="running")
    assert result.returncode == 1
    assert "FAILED at step upload_queued" in result.stderr
    assert "a worker outside WORKER_START is running" in result.stderr
    requests = (tmp_path / "requests.log").read_text(encoding="utf-8")
    assert "/jobs/LATE/cancel" not in requests
    assert calls == ["start", "templates", "stop"]


def _make_e2e_dry_run(repo_root: Path, *extra: str, env: dict[str, str] | None = None) -> str:
    import os

    full = dict(os.environ) | (env or {})
    result = subprocess.run(
        ["make", "-n", "e2e", *extra],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=full,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_make_e2e_sets_worker_hooks(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    block = makefile.split("\ne2e:\n", 1)[1].split("\n\n", 1)[0]
    assert "export WORKER_START='$(E2E_WORKER_START)'" in block
    assert "WORKER_STOP='$(E2E_WORKER_STOP)'" in block
    assert "TEMPLATES_LOAD='$(E2E_TEMPLATES_LOAD)'" in block
    out = _make_e2e_dry_run(repo_root)
    start = out.split("WORKER_START='", 1)[1].split("' WORKER_STOP=", 1)[0]
    assert "APP_ROLE=worker" in start and "uv run --frozen python -m app.workers.main" in start
    assert "&" in start and "echo $! >" in start
    stop = out.split("WORKER_STOP='", 1)[1].split("'", 1)[0]
    assert 'kill -TERM "$pid"' in stop
    load = out.split("TEMPLATES_LOAD='", 1)[1].split("'", 1)[0]
    assert load == 'uv run --frozen python -m app.cli templates load --file "$1"'
    assert ".e2e/" in (repo_root / ".gitignore").read_text(encoding="utf-8")


def test_make_e2e_hooks_honour_overrides(repo_root: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "w.pid"
    out = _make_e2e_dry_run(
        repo_root,
        f"E2E_WORKER_PIDFILE={pidfile}",
        env={"API_HOST_PORT": "8999", "DATABASE_URL": "postgresql+psycopg://x:y@localhost:1/z"},
    )
    start = out.split("WORKER_START='", 1)[1].split("' WORKER_STOP=", 1)[0]
    stop = out.split("WORKER_STOP='", 1)[1].split("'", 1)[0]
    assert str(pidfile) in start and str(pidfile) in stop
    assert 'base="http://127.0.0.1:8999"' in out
    # the hooks set no database URL: the caller's DATABASE_URL / ALEMBIC_DATABASE_URL reach them
    for hook in (start, stop, out.split("TEMPLATES_LOAD='", 1)[1].split("'", 1)[0]):
        assert "DATABASE_URL=" not in hook


def test_bootstrap_runs_import_flow(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    call = 'scripts/e2e/import_flow.sh "http://127.0.0.1:${api_port}"'
    assert code.count(call) == 1
    flow = code.index(call)
    hooks = [
        code.index(f"export {name}=") for name in ("WORKER_START", "WORKER_STOP", "TEMPLATES_LOAD")
    ]
    assert all(h < flow for h in hooks)
    start = code[hooks[0] : hooks[1]]
    assert "-e APP_ROLE=worker" in start and "DATABASE_URL='$app_dsn'" in start
    assert "S3_ENDPOINT_URL=http://minio:9000" in start
    load = code[hooks[2] : flow]
    assert "-e APP_ROLE=shell" in load and "ALEMBIC_DATABASE_URL='$migrator_dsn'" in load
    assert "-m app.cli templates load --file /tmp/templates.json" in load
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    assert (
        lines[lines.index(call) - 1]
        == 'scripts/ci/reset_rate_limits.sh "http://127.0.0.1:${api_port}"'
    )
    assert (
        code.index(E2E_CALL) < flow < code.index('docker rm -f "$e2e_container" >/dev/null', flow)
    )
    assert '"$import_worker"' in code.split("trap cleanup EXIT")[0]
    assert code.index("app.cli dictionary sync") < code.index("app.cli seed --profile demo")


def test_scaffold_import_files_retired(repo_root: Path) -> None:
    for name in ("app/workers/main.py", "app/services/imports/row_model.py"):
        assert not (repo_root / "scaffold/apps/api" / name).exists(), name


def test_prove_gates_has_s13b_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    nan = re.search(r"(?m)^[ \t]*leg api-tests-row-model-nan .*$", text)
    assert nan is not None and "test_row_model_nan_infinity_row_error" in nan.group(0)
    assert 'COMPOSE_PROJECT_NAME="$self_project"' in nan.group(0)
    assert re.search(r"(?m)^leg lint-imports-boto3-outside-adapter lint-imports ", text)
    row_model = (repo_root / "apps/api/app/services/imports/row_model.py").read_text(
        encoding="utf-8"
    )
    seed = text.split("seed_row_model_nan() {", 1)[1].split("\n}", 1)[0]
    old = seed.split("row_model.py '", 1)[1].split("'", 1)[0]
    assert old in row_model  # the seed still applies to today's file


# ---- S20 T21 (AC17): the marketplace preview e2e ----

PREVIEW_E2E = "scripts/e2e/marketplace_preview.sh"
PREVIEW_E2E_STEPS = (
    "health",
    "errors_load",
    "errors_load_again",
    "db_error_map_count_same",
    "login_acme_admin",
    "preview_demo_200",
    "preview_family_200",
    "no_token_401",
    "login_bharat_admin",
    "bharat_preview_404",
)


def _step_bodies(code: str) -> dict[str, str]:
    parts = re.split(r"(?m)^step (\w+)$", code)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_e2e_marketplace_preview_steps(repo_root: Path) -> None:
    from app.db.seed_ops import SEED_NAMESPACE

    code = _code((repo_root / PREVIEW_E2E).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == PREVIEW_E2E_STEPS
    assert re.findall(r"\$\(login (\S+)\)", code) == ["admin@acme.test", "admin@bharat.test"]
    assert not re.search(r":\d{2,5}\b", code)
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert f'seed_namespace="{SEED_NAMESPACE}"' in code
    # psql only in db_error_map_count_same, and only the read-only count
    bodies = _step_bodies(code)
    assert [name for name, body in bodies.items() if "psql" in body] == ["db_error_map_count_same"]
    assert "psql" not in code.split("\nstep ", 1)[0]
    count = bodies["db_error_map_count_same"]
    assert count.count("docker compose exec -T postgres psql") == 2
    assert "count_sql='SELECT count(*) FROM marketplace_error_map'" in count
    assert not re.search(r"(?i)\b(insert|update|delete|truncate|alter|drop)\b", count)
    assert "ERRORS_LOAD" in bodies["errors_load"] and "ERRORS_LOAD" in bodies["errors_load_again"]


def test_e2e_marketplace_preview_in_make_e2e(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "E2E_ERRORS_LOAD = uv run --frozen python -m app.cli errors load --file" in makefile
    assert "ERRORS_LOAD='$(E2E_ERRORS_LOAD)'" in makefile


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_e2e_marketplace_preview_refuses_non_local_url(
    repo_root: Path, tmp_path: Path, url: str
) -> None:
    env, log = _stub_path(tmp_path)
    env["ERRORS_LOAD"] = "true"
    result = subprocess.run(
        [str(repo_root / PREVIEW_E2E), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_e2e_marketplace_preview_without_hooks_exits_2(repo_root: Path, tmp_path: Path) -> None:
    env, log = _stub_path(tmp_path)
    env.pop("ERRORS_LOAD", None)
    result = subprocess.run(
        [str(repo_root / PREVIEW_E2E), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr.startswith("usage: ERRORS_LOAD=<cmd>")
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


S17B_LEGS = {
    "lint-imports-image-child-config": (
        "lint-imports",
        "the image check child imports no app layer, DB, storage or HTTP client BROKEN",
        "seed_image_child_config",
    ),
    "tenant-isolation-images-no-tenant-filter": (
        "tenant-isolation",
        "test_iso_images_repository_reads",
        "seed_images_no_tenant_filter",
    ),
    "api-tests-image-key-prefix": (
        "api-tests",
        "test_runner_rejects_foreign_key_prefix",
        "seed_image_key_prefix",
    ),
}


def test_prove_gates_has_s17b_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    for name, (gate, regex, seed_name) in S17B_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert leg.group(2) == seed_name, name
        assert re.search(rf"(?m)^{seed_name}\(\) \{{", code), seed_name
    docker = code[code.index('if [ "$with_docker" -eq 1 ]; then') :]
    assert "leg tenant-isolation-images-no-tenant-filter" in docker
    assert "leg api-tests-image-key-prefix" in docker


# ---- S17b T18: the image pipeline e2e ----

IMAGE_FLOW = "scripts/e2e/image_pipeline.sh"
IMAGE_STEPS_SIGNED = (
    "upload_url_200",
    "put_signed_200",
    "put_signed_again_412",
    "put_wrong_length_403",
    "register_202",
    "job_succeeded",
    "report_amazon_resolution_low",
    "report_flipkart_pass_with_warning",
    "replay_same_key_same_image",
    "idem_key_other_body_409",
)
IMAGE_STEPS = (
    "health",
    "login_acme_operator",
    "create_product_201",
    "worker_start",
    *IMAGE_STEPS_SIGNED,
    "multipart_seed_main_202",
    "multipart_main_1000_white_png_202",
    "job_succeeded_2",
    "old_main_report_404",
    "product_lists_live_keys",
    "login_acme_reviewer",
    "reviewer_upload_url_403",
    "no_token_401",
    "login_bharat_operator",
    "bharat_report_404",
    "bharat_upload_foreign_key_404",
    "worker_stop",
)


def _run_image_flow(
    repo_root: Path, tmp_path: Path, base: str, *, hooks: bool = True
) -> subprocess.CompletedProcess[str]:
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        f'#!/bin/sh\necho called >> "{tmp_path}/curl.log"\necho 000\n', encoding="utf-8"
    )
    curl.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    for name in ("WORKER_START", "WORKER_STOP"):
        env.pop(name, None)
    if hooks:
        env["WORKER_START"] = "true"
        env["WORKER_STOP"] = "true"
    return subprocess.run(
        [str(repo_root / IMAGE_FLOW), base],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def test_e2e_image_pipeline_steps(repo_root: Path) -> None:
    code = _code((repo_root / IMAGE_FLOW).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^\s*step (\w+)$", code)) == IMAGE_STEPS
    assert code.index("step worker_start") < code.index("step upload_url_200")
    assert code.index("step worker_stop") > code.index("step bharat_upload_foreign_key_404")
    assert "POLL_LIMIT_S=60" in code and "$(( $(date +%s) + POLL_LIMIT_S ))" in code
    assert 'sku="E2E-IMG-$run"' in code and 'run="$(date +%s)-$RANDOM"' in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert not re.search(r":\d{2,5}\b", code)  # no literal port
    assert "--password" not in code and "-u " not in code  # never a password on argv
    assert "uv run --frozen python scripts/e2e/make_image.py" in code


def test_e2e_image_pipeline_refuses_non_local_url(repo_root: Path, tmp_path: Path) -> None:
    result = _run_image_flow(repo_root, tmp_path, "http://api.example.com")
    assert result.returncode == 2
    assert not (tmp_path / "curl.log").exists()
    code = _code((repo_root / IMAGE_FLOW).read_text(encoding="utf-8"))
    signed = code.split("put_signed() {", 1)[1].split("\n}\n", 1)[0]
    assert 'u.hostname in ("127.0.0.1", "localhost")' in signed and "exit 2" in signed


def test_e2e_image_pipeline_without_hooks_exits_2(repo_root: Path, tmp_path: Path) -> None:
    result = _run_image_flow(repo_root, tmp_path, "http://127.0.0.1:8123", hooks=False)
    assert result.returncode == 2
    assert result.stderr.startswith("usage: WORKER_START=<cmd> WORKER_STOP=<cmd>")
    assert not (tmp_path / "curl.log").exists()


def test_bootstrap_runs_image_pipeline_twice_multipart_only(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    call = 'E2E_SIGNED_LEG=0 scripts/e2e/image_pipeline.sh "http://127.0.0.1:${api_port}"'
    assert code.count(call) == 2
    assert code.index("export WORKER_START=") < code.index(call)
    assert code.index('scripts/e2e/import_flow.sh "http://127.0.0.1:${api_port}"') < code.index(
        call
    )


def test_prove_gates_has_s16_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    assert "leg lint-openai-outside-embeddings api-lint 'TID251'" in text
    assert "leg lint-imports-chunker-db lint-imports 'brand chunking is pure BROKEN'" in text
    docker = text.split('if [ "$with_docker" -eq 1 ]; then', 1)[1]
    assert (
        "leg tenant-isolation-brand-ingest-no-tenant-filter tenant-isolation "
        "'test_brand_chunk_search_ingested_isolation' seed_iso_no_filter"
    ) in docker
    for seed in ("seed_openai_in_chunker", "seed_chunker_imports_db"):
        assert f"{seed}() {{" in text


# ---- S16 T22 (AC10): the brand onboarding e2e ----

BRAND_FLOW = "scripts/e2e/brand_onboarding.sh"
BRAND_STEPS = (
    "health",
    "cli_tenant_create",
    "cli_duplicate_slug_fails",
    "login_owner",
    "get_tenant_me_etag",
    "patch_tenant_me_200",
    "patch_stale_409",
    "worker_start",
    "upload_202",
    "job_succeeded_under_30s",
    "list_ready_with_chunks",
    "reupload_new_key_same_job",
    "key_reuse_other_file_409",
    "cli_add_member_reviewer",
    "login_reviewer",
    "reviewer_upload_403",
    "login_bharat_reviewer",
    "bharat_list_lacks",
    "login_bharat_admin",
    "bharat_delete_404",
    "delete_204",
    "delete_again_404",
    "db_profile_chunks_present",
    "db_doc_chunks_inactive",
    "worker_stop",
)
_BRAND_TENANT = "11111111-1111-4111-8111-111111111111"
_BRAND_DOC = "22222222-2222-4222-8222-222222222222"

# A `curl` stand-in for the brand flow: it answers from the URL, method, headers and body (a
# `--data-binary @file` is read), writes the body to `-o`, the headers to `-D`, and prints the
# status. Every argv goes to STUB_DIR/argv.log, every login email to STUB_DIR/logins.log.
_BRAND_CURL_STUB = r"""#!/usr/bin/env python3
import json, os, re, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "argv.log"), "a", encoding="utf-8") as fh:
    fh.write("curl " + " ".join(args) + "\n")
out = hdr = None; method = "GET"; headers = {}; form = ""; url = ""; data = ""
i = 0
while i < len(args):
    a = args[i]
    if a == "-o": out = args[i + 1]; i += 2; continue
    if a == "-D": hdr = args[i + 1]; i += 2; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "-H":
        k, _, v = args[i + 1].partition(": "); headers[k.lower()] = v; i += 2; continue
    if a == "-F": form = args[i + 1]; i += 2; continue
    if a == "--data-binary":
        data = args[i + 1]
        if data.startswith("@"):
            with open(data[1:], encoding="utf-8") as fh:
                data = fh.read()
        i += 2; continue
    if a == "-w": i += 2; continue
    if a.startswith("http"): url = a
    i += 1
def flag(name):
    return os.path.exists(os.path.join(d, name))
def mark(name):
    open(os.path.join(d, name), "w").close()
token = headers.get("authorization", "").removeprefix("Bearer ")
path = re.sub(r"^https?://[^/]+", "", url)
status, body, etag = 404, {"error": {"type": "NOT_FOUND"}}, None
if path == "/readyz":
    status, body = 200, {}
elif path.endswith("/auth/login"):
    who = json.loads(data)["email"]
    with open(os.path.join(d, "logins.log"), "a", encoding="utf-8") as fh:
        fh.write(who + "\n")
    status, body = 200, {"access_token": who}
elif path.endswith("/tenants/me") and method == "GET":
    with open(os.path.join(d, "slug"), encoding="utf-8") as fh:
        slug = fh.read().strip()
    status, body, etag = 200, {"id": "TENANT", "slug": slug}, '"v1"'
elif path.endswith("/tenants/me") and method == "PATCH":
    if headers.get("if-match") == '"v1"' and not flag("patched"):
        mark("patched")
        patch = json.loads(data)
        status, body, etag = 200, patch, '"v2"'
    else:
        status, body = 409, {"error": {"type": "STALE_VERSION"}}
elif path.endswith("/brand/documents") and method == "POST":
    key = headers.get("idempotency-key", "")
    if token.startswith("reviewer-"):
        status, body = 403, {"error": {"type": "FORBIDDEN"}}
    elif "other.md" in form:
        status, body = 409, {"error": {"type": "IDEMPOTENCY_KEY_REUSED"}}
    else:
        status, body = 202, {"job_id": "JOB"}
elif path.endswith("/jobs/JOB"):
    status, body = 200, {"status": "succeeded"}
elif path.endswith("/brand/documents") and method == "GET":
    rows = [] if "bharat" in token else [
        {"id": "DOC", "filename": "brand_guide.md", "status": "ready", "chunk_count": 5}
    ]
    status, body = 200, {"data": rows}
elif path.endswith("/brand/documents/" + os.environ["STUB_DOC"]) and method == "DELETE":
    if token.startswith("owner-") and not flag("deleted"):
        mark("deleted")
        status, body = 204, {}
if out:
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(body).replace("TENANT", os.environ["STUB_TENANT"]).replace("DOC", os.environ["STUB_DOC"]))
if hdr:
    with open(hdr, "w", encoding="utf-8") as fh:
        fh.write("HTTP/1.1 %d\r\n" % status + ("ETag: %s\r\n" % etag if etag else "") + "\r\n")
sys.stdout.write(str(status))
"""

# `docker` (only `compose exec ... psql` here) answers the two db reads; `mkos` is the CLI hook.
_BRAND_DOCKER_STUB = r"""#!/usr/bin/env python3
import os, sys
d = os.environ["STUB_DIR"]
with open(os.path.join(d, "argv.log"), "a", encoding="utf-8") as fh:
    fh.write("docker " + " ".join(sys.argv[1:]) + "\n")
sql = sys.argv[-1]
print("3" if "brand_profile" in sql else "0 5")
"""
_BRAND_CLI_STUB = r"""#!/usr/bin/env python3
import os, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
secret = sys.stdin.readline().rstrip("\n")
with open(os.path.join(d, "argv.log"), "a", encoding="utf-8") as fh:
    fh.write("mkos " + " ".join(args) + "\n")
with open(os.path.join(d, "secrets.log"), "a", encoding="utf-8") as fh:
    fh.write(secret + "\n")
with open(os.path.join(d, "hooks.log"), "a", encoding="utf-8") as fh:
    fh.write("cli-" + args[1] + "\n")
if args[:2] == ["tenant", "create"]:
    slug = args[args.index("--slug") + 1]
    seen = os.path.join(d, "slug")
    if os.path.exists(seen) and open(seen, encoding="utf-8").read().strip() == slug:
        sys.exit(4)
    with open(seen, "w", encoding="utf-8") as fh:
        fh.write(slug)
    print("tenant_id=%s slug=%s" % (os.environ["STUB_TENANT"], slug))
else:
    print("tenant_id=%s user_id=33333333-3333-4333-8333-333333333333" % os.environ["STUB_TENANT"])
"""


def _run_brand_flow(
    repo_root: Path, tmp_path: Path, *, hooks: bool = True
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    for name, text in (
        ("curl", _BRAND_CURL_STUB),
        ("docker", _BRAND_DOCKER_STUB),
        ("mkos-cli", _BRAND_CLI_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(0o755)
    hook_log = stub_dir / "hooks.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env |= {"STUB_DIR": str(stub_dir), "STUB_TENANT": _BRAND_TENANT, "STUB_DOC": _BRAND_DOC}
    for name in ("WORKER_START", "WORKER_STOP", "MKOS_CLI"):
        env.pop(name, None)
    if hooks:
        env["WORKER_START"] = f'echo start >> "{hook_log}"'
        env["WORKER_STOP"] = f'echo stop >> "{hook_log}"'
        env["MKOS_CLI"] = "mkos-cli"
    result = subprocess.run(
        [str(repo_root / BRAND_FLOW), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    return result, stub_dir


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_e2e_brand_onboarding_steps(repo_root: Path, tmp_path: Path) -> None:
    code = _code((repo_root / BRAND_FLOW).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == BRAND_STEPS
    assert "POLL_LIMIT_S=60" in code
    assert 'slug="e2e-$(date +%s)-$RANDOM"' in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert not re.search(r":\d{2,5}\b", code)
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    assert "TRUNCATE" not in code.upper()
    assert "rate_limit_counters" not in code and "RATE_LIMIT_" not in code
    # psql (and any docker call) only in the two read-only db_ steps
    current, hits = "setup", []
    for line in code.splitlines():
        match = re.match(r"^step (\w+)$", line)
        if match:
            current = match.group(1)
        if "psql" in line or "docker" in line:
            hits.append(current)
    assert sorted(set(hits)) == ["db_doc_chunks_inactive", "db_profile_chunks_present"]

    result, stub_dir = _run_brand_flow(repo_root, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout.strip().splitlines()[-1].startswith("e2e: brand_onboarding OK (tenant e2e-")
    )
    assert _lines(stub_dir / "hooks.log") == [
        "cli-create",
        "cli-create",
        "start",
        "cli-add-member",
        "stop",
    ]
    # no password (the CLI's stdin lines) on any argv; each new user's is its own
    secrets = _lines(stub_dir / "secrets.log")
    assert len(secrets) == 3 and len(set(secrets)) == 2 and all(len(s) >= 24 for s in secrets)
    argv = (stub_dir / "argv.log").read_text(encoding="utf-8")
    assert all(secret not in argv for secret in secrets)
    assert "devpassword123" not in argv
    # every email logs in at most 5 times (the limiter's budget); here once each
    logins = _lines(stub_dir / "logins.log")
    assert len(logins) == 4 and len(set(logins)) == 4
    assert {"reviewer@bharat.test", "admin@bharat.test"} <= set(logins)


def test_e2e_brand_onboarding_slug_unique_per_run(repo_root: Path, tmp_path: Path) -> None:
    first, one = _run_brand_flow(repo_root, tmp_path / "a")
    second, two = _run_brand_flow(repo_root, tmp_path / "b")
    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    assert (one / "slug").read_text(encoding="utf-8") != (two / "slug").read_text(encoding="utf-8")


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_e2e_brand_onboarding_refuses_non_local_url(
    repo_root: Path, tmp_path: Path, url: str
) -> None:
    env, log = _stub_path(tmp_path)
    env |= {"WORKER_START": "true", "WORKER_STOP": "true", "MKOS_CLI": "docker"}
    result = subprocess.run(
        [str(repo_root / BRAND_FLOW), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_e2e_brand_onboarding_without_hooks_exits_2(repo_root: Path, tmp_path: Path) -> None:
    result, stub_dir = _run_brand_flow(repo_root, tmp_path, hooks=False)
    assert result.returncode == 2
    assert result.stderr.startswith("usage: WORKER_START=<cmd> WORKER_STOP=<cmd> MKOS_CLI=")
    assert not (stub_dir / "argv.log").exists()


def test_bootstrap_runs_brand_onboarding_twice(repo_root: Path) -> None:
    code = _bootstrap_code(repo_root)
    call = 'scripts/e2e/brand_onboarding.sh "http://127.0.0.1:${api_port}"'
    assert code.count(call) == 2
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    runs = [i for i, line in enumerate(lines) if line == call]
    for i in runs:
        assert lines[i - 1] == RESET_CALL
    hook = code.index("export MKOS_CLI=")
    cli = code[hook : code.index("\n", hook)]
    assert "docker run -i --rm" in cli and "-e APP_ROLE=shell" in cli
    assert "ALEMBIC_DATABASE_URL='$migrator_dsn'" in cli and cli.endswith('-m app.cli"')
    flow = code.index('scripts/e2e/import_flow.sh "http://127.0.0.1:${api_port}"')
    start = code.index("export WORKER_START=")
    assert start < flow < hook < code.index(call)
    assert code.rindex(call) < code.index('docker rm -f "$e2e_container" >/dev/null', flow)


def test_make_e2e_sets_mkos_cli_hook(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    block = makefile.split("\ne2e:\n", 1)[1].split("\n\n", 1)[0]
    assert "MKOS_CLI='$(E2E_MKOS_CLI)'" in block
    # S13b's and S15's hooks stay
    for kept in ("WORKER_START=", "WORKER_STOP=", "TEMPLATES_LOAD=", "FAKE_URL=", "REFRESH_ONCE="):
        assert kept in block, kept
    out = _make_e2e_dry_run(repo_root)
    cli = out.split("MKOS_CLI='", 1)[1].split("'", 1)[0]
    assert cli == "uv run --frozen python -m app.cli"


S18_LEGS = {
    "lint-imports-budget-not-allowlisted": ("lint-imports", ": R5 ", "seed_budget_not_allowlisted"),
    "tenant-isolation-listings-no-tenant-filter": (
        "tenant-isolation",
        "test_iso_generation_repositories_other_tenant_empty",
        "seed_listings_no_tenant_filter",
    ),
    "api-tests-generation-publishes": (
        "api-tests",
        "test_generation_never_publishes",
        "seed_generation_publishes",
    ),
    "api-tests-validate-rules-cap": (
        "api-tests",
        "test_validate_rules_port_call_cap",
        "seed_validate_rules_uncapped",
    ),
}


def test_prove_gates_has_s18_legs(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    code = _code(text)
    for name, (gate, regex, seed_name) in S18_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert leg.group(2) == seed_name, name
        assert re.search(rf"(?m)^{seed_name}\(\) \{{", code), seed_name


# ---- S18 T21 (AC 17): generate.sh and regenerate.sh ----

GENERATE_FLOW = "scripts/e2e/generate.sh"
REGENERATE_FLOW = "scripts/e2e/regenerate.sh"
GENERATE_STEPS = (
    "health",
    "login_acme_operator",
    "create_product_201",
    "generate_202",
    "replay_same_202",
    "new_key_busy_409",
    "worker_start",
    "poll_job_succeeded",
    "status_validated",
    "login_acme_reviewer",
    "reviewer_generate_403",
    "login_bharat_operator",
    "bharat_status_404",
    "db_version_and_runs",
    "create_product_failed_201",
    "generate_stub_failed_202",
    "poll_job_failed",
    "status_failed_generation_failed",
    "job_result_no_version",
    "db_failed_runs",
    "worker_stop",
)
REGENERATE_STEPS = (
    "health",
    "login_acme_operator",
    "create_product_201",
    "worker_start",
    "generate_stub_blocked_202",
    "poll_blocked",
    "job_error_present",
    "login_acme_reviewer",
    "reviewer_regenerate_202",
    "poll_validated",
    "db_maker_is_reviewer",
    "worker_stop",
)
LISTING_EDIT_FLOW = "scripts/e2e/listing_edit.sh"
LISTING_EDIT_STEPS = (
    "health",
    "login_acme_operator",
    "create_product_201",
    "worker_start",
    "generate_202",
    "poll_validated",
    "patch_content_200",
    "patch_stale_409",
    "patch_long_title_blocked",
    "validate_passed_false",
    "validate_again_same_run",
    "worker_stop",
)
S18_FLOWS = {
    GENERATE_FLOW: GENERATE_STEPS,
    REGENERATE_FLOW: REGENERATE_STEPS,
    LISTING_EDIT_FLOW: LISTING_EDIT_STEPS,
}


def _steps(code: str) -> tuple[str, ...]:
    return tuple(re.findall(r"(?m)^\s*step (\w+)$", code))


def _run_flow(
    repo_root: Path, tmp_path: Path, flow: str, base: str, *, hooks: bool = True
) -> subprocess.CompletedProcess[str]:
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        f'#!/bin/sh\necho called >> "{tmp_path}/curl.log"\necho 000\n', encoding="utf-8"
    )
    curl.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    for name in ("WORKER_START", "WORKER_STOP"):
        env.pop(name, None)
    if hooks:
        env["WORKER_START"] = "true"
        env["WORKER_STOP"] = "true"
    return subprocess.run(
        [str(repo_root / flow), base],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("flow", sorted(S18_FLOWS))
def test_e2e_generate_steps(repo_root: Path, flow: str) -> None:
    code = _code((repo_root / flow).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert _steps(code) == S18_FLOWS[flow]
    assert "POLL_LIMIT_S=60" in code and "$(( $(date +%s) + POLL_LIMIT_S ))" in code
    assert '"E2E-GEN-$(date +%s)-$RANDOM"' in code  # a fresh SKU per product, per run
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert not re.search(r":\d{2,5}\b", code)  # no literal port
    assert "--password" not in code and "-u " not in code  # never a password on argv
    assert len(re.findall(r"(?m)^\s*step login_", code)) <= 3


def test_e2e_regenerate_steps(repo_root: Path) -> None:
    code = _code((repo_root / REGENERATE_FLOW).read_text(encoding="utf-8"))
    assert _steps(code) == REGENERATE_STEPS
    assert "stub:blocked" in code


def test_e2e_generate_worker_starts_after_busy_check(repo_root: Path) -> None:
    steps = _steps(_code((repo_root / GENERATE_FLOW).read_text(encoding="utf-8")))
    start = steps.index("worker_start")
    for name in ("generate_202", "replay_same_202", "new_key_busy_409"):
        assert steps.index(name) < start, name


def test_e2e_generate_failed_leg_after_worker_start(repo_root: Path) -> None:
    steps = _steps(_code((repo_root / GENERATE_FLOW).read_text(encoding="utf-8")))
    leg = steps[steps.index("create_product_failed_201") : steps.index("worker_stop")]
    assert leg == (
        "create_product_failed_201",
        "generate_stub_failed_202",
        "poll_job_failed",
        "status_failed_generation_failed",
        "job_result_no_version",
        "db_failed_runs",
    )
    assert steps.index("worker_start") < steps.index("db_version_and_runs") < steps.index(leg[0])
    assert not any(step.startswith("login_") for step in leg)


@pytest.mark.parametrize("flow", [GENERATE_FLOW, REGENERATE_FLOW])
def test_e2e_s18_psql_only_in_db_steps(repo_root: Path, flow: str) -> None:
    current = "setup"
    hits: list[tuple[str, str]] = []
    for line in _code((repo_root / flow).read_text(encoding="utf-8")).splitlines():
        match = re.match(r"^step (\w+)$", line)
        if match:
            current = match.group(1)
        if "psql" in line or "docker compose exec" in line or re.search(r"\bsql \"", line):
            hits.append((current, line.strip()))
    assert hits
    # the `sql` helper is defined in setup and called from db_* steps only; it reads read-only
    assert all(step == "setup" or step.startswith("db_") for step, _ in hits), hits
    assert "default_transaction_read_only = on" in _code(
        (repo_root / flow).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("flow", sorted(S18_FLOWS))
def test_e2e_generate_refuses_non_local_url(repo_root: Path, tmp_path: Path, flow: str) -> None:
    result = _run_flow(repo_root, tmp_path, flow, "http://api.example.com")
    assert result.returncode == 2
    assert not (tmp_path / "curl.log").exists()


@pytest.mark.parametrize("flow", sorted(S18_FLOWS))
def test_e2e_generate_without_hooks_exits_2(repo_root: Path, tmp_path: Path, flow: str) -> None:
    result = _run_flow(repo_root, tmp_path, flow, "http://127.0.0.1:8123", hooks=False)
    assert result.returncode == 2
    assert result.stderr.startswith("usage: WORKER_START=<cmd> WORKER_STOP=<cmd>")
    assert not (tmp_path / "curl.log").exists()


def test_e2e_listing_edit_http_only(repo_root: Path) -> None:
    code = _code((repo_root / LISTING_EDIT_FLOW).read_text(encoding="utf-8"))
    assert "psql" not in code and "docker compose" not in code


# ---- S19 T15 (AC 16): review_approve.sh and review_edit_reject.sh ----

REVIEW_APPROVE_FLOW = "scripts/e2e/review_approve.sh"
REVIEW_EDIT_REJECT_FLOW = "scripts/e2e/review_edit_reject.sh"
REVIEW_APPROVE_STEPS = (
    "health",
    "login_acme_owner",
    "create_product_201",
    "worker_start",
    "owner_generate_202",
    "poll_validated",
    "owner_approve_maker_checker_403",
    "login_acme_operator",
    "operator_approve_permission_403",
    "login_acme_reviewer",
    "reviewer_approve_200",
    "replay_same_key_identical",
    "not_submitted",
    "history_denied_and_approved",
    "review_queue_lists_validated",
    "login_bharat_reviewer",
    "bharat_get_404",
    "worker_stop",
)
REVIEW_EDIT_REJECT_STEPS = (
    "health",
    "login_acme_operator",
    "create_product_201",
    "worker_start",
    "operator_generate_202",
    "poll_validated",
    "get_etag",
    "patch_with_etag_200",
    "patch_old_etag_409",
    "login_acme_owner",
    "owner_edit_200",
    "owner_approve_editor_403",
    "login_acme_reviewer",
    "reject_without_feedback_422",
    "reject_regenerate_200",
    "poll_regenerated_validated",
    "reviewer_approve_regenerator_403",
    "owner_approve_200",
    "worker_stop",
)
S19_FLOWS = {
    REVIEW_APPROVE_FLOW: REVIEW_APPROVE_STEPS,
    REVIEW_EDIT_REJECT_FLOW: REVIEW_EDIT_REJECT_STEPS,
}


@pytest.mark.parametrize("flow", sorted(S19_FLOWS))
def test_review_e2e_steps(repo_root: Path, flow: str) -> None:
    code = _code((repo_root / flow).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert _steps(code) == S19_FLOWS[flow]
    assert "POLL_LIMIT_S=60" in code and "$(( $(date +%s) + POLL_LIMIT_S ))" in code
    assert '"E2E-GEN-$(date +%s)-$RANDOM"' in code  # a fresh SKU per run
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code
    assert not re.search(r":\d{2,5}\b", code)  # no literal port
    assert "--password" not in code and "-u " not in code


@pytest.mark.parametrize("flow", sorted(S19_FLOWS))
def test_review_e2e_refuses_non_local_url(repo_root: Path, tmp_path: Path, flow: str) -> None:
    result = _run_flow(repo_root, tmp_path, flow, "http://api.example.com")
    assert result.returncode == 2
    assert not (tmp_path / "curl.log").exists()


@pytest.mark.parametrize("flow", sorted(S19_FLOWS))
def test_review_e2e_logins_le_5_per_email(repo_root: Path, flow: str) -> None:
    code = _code((repo_root / flow).read_text(encoding="utf-8"))
    emails = re.findall(r"login ([a-z]+@[a-z]+\.test)\)", code)
    assert emails
    assert max(emails.count(e) for e in set(emails)) <= 5


@pytest.mark.parametrize("flow", sorted(S19_FLOWS))
def test_review_e2e_no_db_access(repo_root: Path, flow: str) -> None:
    code = _code((repo_root / flow).read_text(encoding="utf-8"))
    assert "psql" not in code and "docker compose" not in code
    assert "TRUNCATE" not in code.upper()


def test_review_e2e_proves_maker_checker_over_http(repo_root: Path) -> None:
    """A second user approves, the generating and the editing user are refused."""
    approve = _code((repo_root / REVIEW_APPROVE_FLOW).read_text(encoding="utf-8"))
    edit = _code((repo_root / REVIEW_EDIT_REJECT_FLOW).read_text(encoding="utf-8"))
    assert approve.count('"MAKER_CHECKER_VIOLATION"') == 1
    assert edit.count('"MAKER_CHECKER_VIOLATION"') == 2


S19A_LEGS = {
    "api-tests-no-api-maker-checker": ("api-tests", "test_api_maker_checker_refuses_before_sql"),
    "api-tests-no-if-match": ("api-tests", "test_patch_stale_409"),
    "api-tests-approve-image-gate": ("api-tests", "test_approve_blocked_unchecked_image"),
}


def test_prove_gates_s19a_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (gate, regex) in S19A_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
        assert code.index(f"leg {name}") > code.index('if [ "$with_docker" -eq 1 ]; then')
    review = (repo_root / "apps/api/app/services/review.py").read_text(encoding="utf-8")
    edit = (repo_root / "apps/api/app/services/listing_edit.py").read_text(encoding="utf-8")
    # every seed's <old> text is still in the source, so the seed applies
    assert (
        "if approver_refusal(approver, await _chain(ctx, listing.id, version.id)) is not None:"
        in review
    )
    assert "    _image_gate(ctx, listing, version)" in review
    assert "    if if_match != listing.current_version_id:" in edit


# ---- S19 T23 (AC 16): issues.sh ----

ISSUES_FLOW = "scripts/e2e/issues.sh"
ISSUES_STEPS = (
    "health",
    "login_acme_operator",
    "create_products_201",
    "worker_start",
    "generate_stub_warnings_202",
    "poll_needs_review",
    "issues_one_open_warning",
    "login_acme_reviewer",
    "reviewer_dismiss_200",
    "replay_identical",
    "generate_stub_blocked_202",
    "poll_blocked",
    "error_issue_open",
    "operator_dismiss_403",
    "reviewer_dismiss_error_409",
    "tasks_200",
    "login_bharat_reviewer",
    "bharat_issues_lack_acme",
    "worker_stop",
)


def test_issues_e2e_steps(repo_root: Path) -> None:
    code = _code((repo_root / ISSUES_FLOW).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert _steps(code) == ISSUES_STEPS
    assert "stub:$2" in code and "warnings" in code and "blocked" in code
    assert '"E2E-GEN-$(date +%s)-$RANDOM"' in code
    assert 'cat "$body"' not in code
    assert not re.search(r":\d{2,5}\b", code)


def test_issues_e2e_refuses_non_local_url(repo_root: Path, tmp_path: Path) -> None:
    result = _run_flow(repo_root, tmp_path, ISSUES_FLOW, "http://api.example.com")
    assert result.returncode == 2
    assert not (tmp_path / "curl.log").exists()


def test_issues_e2e_logins_le_5_per_email(repo_root: Path) -> None:
    code = _code((repo_root / ISSUES_FLOW).read_text(encoding="utf-8"))
    emails = re.findall(r"login ([a-z]+@[a-z]+\.test)\)", code)
    assert emails and max(emails.count(e) for e in set(emails)) <= 5


def test_issues_e2e_no_db_access(repo_root: Path) -> None:
    code = _code((repo_root / ISSUES_FLOW).read_text(encoding="utf-8"))
    assert "psql" not in code and "docker compose" not in code
    assert "TRUNCATE" not in code.upper()


S19B_LEGS = {
    "api-tests-dismiss-error-precheck": ("api-tests", "test_issue_dismiss_error_409"),
    "tenant-isolation-issues-unscoped": (
        "tenant-isolation",
        "test_iso_issue_and_task_reads_other_tenant_empty",
    ),
}


def test_prove_gates_s19b_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (gate, regex) in S19B_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
    issues = (repo_root / "apps/api/app/services/issues.py").read_text(encoding="utf-8")
    repo = (repo_root / "apps/api/app/repositories/listing_issues.py").read_text(encoding="utf-8")
    assert 'if issue.status != "open" or issue.severity != "warning":' in issues
    assert "stmt = self._select().where(ListingIssue.status == filters.status)" in repo


# ---- S21 T25, T26 (AC15): the publish e2e scripts ----

PUBLISH_E2E = {
    "scripts/e2e/publish_flow.sh": (
        "health",
        "fake_health",
        "errors_load",
        "login_acme_owner",
        "cleanup_live_amazon",
        "connect_confirm",
        "login_acme_operator",
        "login_acme_reviewer",
        "worker_start",
        "create_generate_approve",
        "reviewer_publish_403",
        "publish_202",
        "publish_replay_identical",
        "publish_other_body_409",
        "poll_published_processing",
        "fake_set_buyable",
        "refresh_status_202",
        "refresh_again_429",
        "poll_live",
        "create_generate_approve_2",
        "fake_fail_two_fields",
        "publish_2",
        "poll_failed_partial",
        "submissions_2_fields_failed",
        "retry_fields_202",
        "poll_published_2",
        "fake_calls_only_two_paths",
        "no_token_401",
        "login_bharat_operator",
        "bharat_publish_item_404",
        "bharat_submissions_404",
        "worker_stop",
        "revoke_account",
    ),
    "scripts/e2e/publish_recovery.sh": (
        "health",
        "fake_health",
        "errors_load",
        "login_acme_owner",
        "cleanup_live_amazon",
        "connect_confirm",
        "login_acme_operator",
        "login_acme_reviewer",
        "worker_start",
        "create_generate_approve",
        "fake_auth_expired",
        "publish_202",
        "poll_blocked_action_required",
        "publish_blocked_until_reconnect_409",
        "reconnect",
        "publish_resume_202",
        "poll_published",
        "create_generate_approve_2",
        "fake_429_always",
        "publish_until_exhausted",
        "poll_blocked_attempts_exhausted",
        "publish_cap_409",
        "retry_fields_cap_409",
        "fake_reset",
        "worker_stop",
        "revoke_account",
    ),
}


@pytest.mark.parametrize("script", sorted(PUBLISH_E2E))
def test_e2e_publish_scripts_steps(repo_root: Path, script: str) -> None:
    code = _code((repo_root / script).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == PUBLISH_E2E[script]
    logins = re.findall(r"\$\(login (\S+)\)", code)
    assert len(logins) == len(set(logins)), logins  # one login per seeded email (the limiter)
    assert not re.search(r":\d{2,5}\b", code)
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code and "psql" not in code
    # each script connects its own account (its own seller) and revokes it, so it stands alone
    assert "connect_confirm" in code and "revoke_account" in code
    assert "__control/reset" in code


@pytest.mark.parametrize("script", sorted(PUBLISH_E2E))
@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_e2e_publish_scripts_refuse_non_local_url(
    repo_root: Path, tmp_path: Path, script: str, url: str
) -> None:
    env, log = _stub_path(tmp_path)
    env.update(
        {
            "FAKE_URL": "http://127.0.0.1:4999",
            "WORKER_START": "true",
            "WORKER_STOP": "true",
            "ERRORS_LOAD": "true",
        }
    )
    result = subprocess.run(
        [str(repo_root / script), url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("script", sorted(PUBLISH_E2E))
def test_e2e_publish_scripts_without_hooks_exit_2(
    repo_root: Path, tmp_path: Path, script: str
) -> None:
    env, log = _stub_path(tmp_path)
    for name in ("FAKE_URL", "WORKER_START", "WORKER_STOP", "ERRORS_LOAD"):
        env.pop(name, None)
    result = subprocess.run(
        [str(repo_root / script), "http://127.0.0.1:8123"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr.startswith("usage: FAKE_URL=<fake>")
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


S22_LEGS = {
    "lint-imports-flipkart-adapter-from-service": (
        "lint-imports",
        "only app.adapters and app.workers reach the marketplace adapter and client BROKEN",
    ),
    "lint-imports-flipkart-mapping-httpx": (
        "lint-imports",
        "adapter mapping, endpoint check, error map and registry do no I/O BROKEN",
    ),
}


def test_prove_gates_s22_legs(repo_root: Path) -> None:
    code = _code((repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8"))
    for name, (gate, regex) in S22_LEGS.items():
        leg = re.search(rf"(?m)^[ \t]*leg {re.escape(name)} {gate} '([^']*)' (\S+)", code)
        assert leg is not None, name
        assert leg.group(1) == regex, name
    assert "import app.adapters.flipkart.adapter" in code
    assert "app/adapters/flipkart/mapping.py" in code
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    for name in S22_LEGS:
        assert code.index(f"leg {name}") < docker, name


FLIPKART_E2E = "scripts/e2e/flipkart_connect_preview.sh"


FLIPKART_E2E_STEPS = (
    "health",
    "fake_health",
    "off_health",
    "login_acme_owner",
    "fake_reset_calls",
    "off_connect_501",
    "fake_calls_0",
    "cleanup_live_flipkart",
    "connect_200",
    "consent",
    "callback_303",
    "confirm_200",
    "list_active",
    "preview_family_200",
    "login_bharat_owner",
    "bharat_preview_404",
    "no_token_401",
    "revoke_200",
)


def test_e2e_flipkart_connect_preview_steps(repo_root: Path) -> None:
    from app.db.seed_ops import SEED_NAMESPACE

    code = _code((repo_root / FLIPKART_E2E).read_text(encoding="utf-8"))
    assert "set -euo pipefail" in code
    assert tuple(re.findall(r"(?m)^step (\w+)$", code)) == FLIPKART_E2E_STEPS
    logins = re.findall(r"\$\(login (\S+)\)", code)
    assert logins == ["owner@acme.test", "owner@bharat.test"]
    assert len(set(logins)) == len(logins)  # one login per email
    assert not re.search(r":\d{2,5}\b", code)
    assert "scripts/ci/worktree_env.sh exports .worktree.env" in code
    assert "FAILED at step $current (HTTP status ${status:-none})" in code
    assert 'cat "$body"' not in code and "psql" not in code
    assert f'seed_namespace="{SEED_NAMESPACE}"' in code
    bodies = _step_bodies(code)
    assert "$off_api/marketplaces/connect" in bodies["off_connect_501"]
    assert "__calls?marketplace=flipkart" in bodies["fake_calls_0"]
    assert "acme:demo-flipkart:KURTA-FAM-01" in code


@pytest.mark.parametrize("url", NON_LOCAL_URLS)
def test_e2e_flipkart_connect_preview_refuses_non_local_url(
    repo_root: Path, tmp_path: Path, url: str
) -> None:
    env, log = _stub_path(tmp_path)
    env.update(FAKE_URL="http://127.0.0.1:4020", FLAG_OFF_URL="http://127.0.0.1:8011")
    for args, extra in (([url], {}), (["http://127.0.0.1:8123"], {"FLAG_OFF_URL": url})):
        result = subprocess.run(
            [str(repo_root / FLIPKART_E2E), *args],
            capture_output=True,
            text=True,
            env={**env, **extra},
            timeout=60,
            check=False,
        )
        assert result.returncode == 2, result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_e2e_flipkart_connect_preview_without_hooks_exits_2(
    repo_root: Path, tmp_path: Path
) -> None:
    env, log = _stub_path(tmp_path)
    for missing in ("FAKE_URL", "FLAG_OFF_URL"):
        hooks = {"FAKE_URL": "http://127.0.0.1:4020", "FLAG_OFF_URL": "http://127.0.0.1:8011"}
        del hooks[missing]
        run_env = {k: v for k, v in env.items() if k not in ("FAKE_URL", "FLAG_OFF_URL")}
        result = subprocess.run(
            [str(repo_root / FLIPKART_E2E), "http://127.0.0.1:8123"],
            capture_output=True,
            text=True,
            env={**run_env, **hooks},
            timeout=60,
            check=False,
        )
        assert result.returncode == 2
        assert result.stderr.startswith("usage: FAKE_URL=<fake> FLAG_OFF_URL=<flag-off API>")
    assert not log.exists() or log.read_text(encoding="utf-8") == ""
