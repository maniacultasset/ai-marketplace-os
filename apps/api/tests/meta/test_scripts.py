"""Supports AC11/AC15: every CI shell script parses and fails fast."""

from __future__ import annotations

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
    ops = [o for o in OPS.values() if o.method == "GET" and o.path == probe.group(1)]
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
    assert "AND token_enc IS NULL" in code and "0007_oauth_pending.py" in code
    assert "SET status = 'revoked', token_enc = NULL" in code
    # the migration leg re-migrates mos_iso afterwards with the checkout's own revisions
    hash_leg = code.index("leg tenant-isolation-hash-branch-pending")
    assert code.index("remigrate_iso >>", hash_leg) > hash_leg
    docker = code.index('if [ "$with_docker" -eq 1 ]; then')
    assert code.index("leg api-tests-revoke-no-shred") > docker
    assert hash_leg > docker
    assert code.index("leg lint-imports-unseal-from-router") < docker
