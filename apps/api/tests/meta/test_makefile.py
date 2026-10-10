"""Every `uv run` is frozen, and `make hooks` is the install path (P19)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

_BARE_UV_RUN = re.compile(r"\buv\s+run\b(?!\s+--frozen(?:\s|$))")


def bare_uv_runs(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines(), start=1) if _BARE_UV_RUN.search(line)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("uv run pytest", [1]),
        ("\tuv  run mypy", [1]),
        ("uv run --frozen-lockfile x", [1]),
        ("x && uv run y", [1]),
        ("uv run --frozen ruff", []),
        ("uv sync --frozen", []),
        ("", []),
        ("\n", []),
    ],
)
def test_bare_uv_run_detector_flags_seeded_lines(text: str, expected: list[int]) -> None:
    assert bare_uv_runs(text) == expected


def test_every_uv_run_is_frozen(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert bare_uv_runs(makefile) == []
    for script in sorted((repo_root / "scripts" / "ci").glob("*.sh")):
        assert bare_uv_runs(script.read_text(encoding="utf-8")) == [], script


def test_hooks_target_installs_and_dev_calls_it(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^\.PHONY:\s+hooks\s*$", text, re.MULTILINE)
    hooks = re.search(r"^hooks:\n\t(.+)\n", text, re.MULTILINE)
    assert hooks is not None
    assert hooks.group(1) == "uv run --frozen pre-commit install"
    dev = re.search(r"^dev:\n((?:\t.+\n)+)", text, re.MULTILINE)
    assert dev is not None
    body = dev.group(1)
    assert "uv sync --frozen --all-packages" in body
    sync_at = body.index("uv sync --frozen --all-packages")
    assert "$(MAKE) hooks" in body[sync_at:]
    assert "pre-commit install" not in body


def test_help_lists_hooks(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    help_echo = next(line for line in text.splitlines() if "make dev" in line and "echo" in line)
    assert "hooks" in help_echo


# ---- S5: the Alembic round trip and squawk before pytest (CANON.md:136); `make dev` upgrades ----


def _dry_run(repo_root: Path, target: str) -> list[str]:
    import os
    import subprocess

    env = {**os.environ, "CI": "true"}  # CI skips the compose port guard
    result = subprocess.run(
        ["make", "-n", "-C", str(repo_root), target],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.splitlines()


def _first(lines: list[str], needle: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if needle in lines[index]:
            return index
    raise AssertionError(f"{needle!r} not found after line {start}: {lines}")


def test_api_tests_runs_alembic_sequence_before_pytest(repo_root: Path) -> None:
    lines = _dry_run(repo_root, "api-tests")
    alembic = "alembic -c apps/api/alembic.ini"
    at = _first(lines, f"{alembic} upgrade head")
    assert "--sql" not in lines[at]
    at = _first(lines, f"{alembic} check", at + 1)
    at = _first(lines, f"{alembic} downgrade base", at + 1)
    at = _first(lines, f"{alembic} upgrade head", at + 1)
    assert "--sql" not in lines[at]
    at = _first(lines, f"{alembic} upgrade head --sql", at + 1)
    at = _first(lines, "squawk_report.py", at)  # the same shell line, after the render
    _first(lines, "pytest", at + 1)


def test_api_tests_sets_alembic_url_itself(repo_root: Path, load_yaml: Any) -> None:
    lines = _dry_run(repo_root, "api-tests")
    alembic_lines = [line for line in lines if "alembic -c apps/api/alembic.ini" in line]
    assert len(alembic_lines) == 5
    for line in alembic_lines:
        assert "ALEMBIC_DATABASE_URL=postgresql+psycopg://" in line
        assert re.search(r"ALEMBIC_DATABASE_URL=\S+/mos_test\b", line), line
    workflow = load_yaml(repo_root / ".github/workflows/ci-api.yml")
    for job_id, job in workflow["jobs"].items():
        env = job.get("env")
        if env is not None:
            assert sorted(env) == ["APP_ENV", "DATABASE_URL", "JWT_SECRET"], job_id


def test_dev_upgrades_after_bootstrap_db_and_sync(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    dev = re.search(r"^dev:\n((?:\t.+\n)+)", text, re.MULTILINE)
    assert dev is not None
    body = dev.group(1)
    bootstrap = body.index("$(MAKE) bootstrap-db")
    sync = body.index("uv sync --frozen --all-packages")
    upgrade = body.index("uv run --frozen alembic -c apps/api/alembic.ini upgrade head")
    serve = body.index("uvicorn")
    assert bootstrap < sync < upgrade < serve


# ---- S9 D14: the CI-only app_rw password reaches 0000_role_passwords.sql as a SCRAM verifier ----


def _target(makefile: str, name: str) -> str:
    return makefile.split(f"\n{name}:\n", 1)[1].split("\n\n", 1)[0]


def test_test_db_sets_app_rw_scram_from_constant(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "\nCI_APP_RW_PASSWORD := ci_only_app_rw_not_a_real_password\n" in makefile
    assert (
        "\nAPP_RW_SCRAM_CMD = printf '%s' '$(CI_APP_RW_PASSWORD)' | "
        "uv run --frozen python scripts/db/scram_verifier.py\n" in makefile
    )
    assert _target(makefile, "app-rw-scram").strip() == "@$(APP_RW_SCRAM_CMD)"
    test_db = _target(makefile, "test-db")
    assert 'APP_RW_SCRAM="$$($(APP_RW_SCRAM_CMD))"' in test_db
    assert "-e APP_RW_SCRAM postgres" in test_db
    assert "-f - < scripts/db/0000_role_passwords.sql" in test_db
    # the plaintext never sits on a psql argv: only the constant's printf feeds the helper's stdin
    for line in test_db.splitlines():
        if "psql" in line:
            assert "CI_APP_RW_PASSWORD" not in line and "ci_only_app_rw" not in line


def test_bootstrap_db_sets_local_role_passwords(repo_root: Path) -> None:
    """S10 C14: the local app logs in as app_rw and Alembic as migrator, so bootstrap-db sets
    their local dev-only passwords (as SCRAM verifiers) unless the caller passes its own."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "\nCI_MIGRATOR_PASSWORD := ci_only_migrator_not_a_real_password\n" in makefile
    assert (
        "\nMIGRATOR_SCRAM_CMD = printf '%s' '$(CI_MIGRATOR_PASSWORD)' | "
        "uv run --frozen python scripts/db/scram_verifier.py\n" in makefile
    )
    recipe = _target(makefile, "bootstrap-db")
    assert 'APP_RW_SCRAM="$${APP_RW_SCRAM:-$$($(APP_RW_SCRAM_CMD))}"' in recipe
    assert 'MIGRATOR_SCRAM="$${MIGRATOR_SCRAM:-$$($(MIGRATOR_SCRAM_CMD))}"' in recipe
    assert "-e APP_RW_SCRAM -e APP_RO_SCRAM -e MIGRATOR_SCRAM postgres" in recipe
    for line in recipe.splitlines():
        if "psql" in line:
            assert "_PASSWORD" not in line and "ci_only_" not in line


def test_tenant_isolation_migrates_then_runs_as_app_rw(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert (
        "\nISO_ADMIN_URL ?= postgresql+psycopg://postgres:postgres@localhost:$(PG_HOST_PORT)/mos_iso\n"
        in makefile
    )
    assert (
        "\nISO_DATABASE_URL ?= postgresql+psycopg://app_rw:$(CI_APP_RW_PASSWORD)@localhost:"
        "$(PG_HOST_PORT)/mos_iso\n" in makefile
    )
    recipe = _target(makefile, "tenant-isolation")
    migrate = recipe.index(
        "APP_ENV=local APP_ROLE=migrate ALEMBIC_DATABASE_URL=$(ISO_ADMIN_URL) "
        "uv run --frozen python -m app.cli db migrate"
    )
    pytest_at = recipe.index("uv run --frozen pytest -q -m tenant_isolation --no-cov")
    assert migrate < pytest_at
    run = recipe[migrate:]
    for part in (
        "APP_ENV=local",
        "DATABASE_URL=$(ISO_DATABASE_URL)",
        "ALEMBIC_DATABASE_URL=$(ISO_ADMIN_URL)",
        "JWT_SECRET=$(CI_JWT_SECRET)",
    ):
        assert part in run
    assert '-m "not tenant_isolation and not cloudsql_mimic"' in _target(makefile, "api-tests")


def test_no_sentinel_left(repo_root: Path) -> None:
    folder = repo_root / "apps/api/tests/tenant_isolation"
    assert not (folder / "test_sentinel.py").exists()
    assert (folder / "conftest.py").is_file() and (folder / "test_app_rw.py").is_file()


# ---- S14a: the rules_engine branch-coverage gate (ADR-0010 §2; CODE-PLAN T14) ----


def test_api_tests_gates_rules_engine_branch_coverage(repo_root: Path) -> None:
    lines = _dry_run(repo_root, "api-tests")
    at = _first(lines, "pytest -q")
    at = _first(lines, "coverage json", at + 1)
    assert "--include='*/app/services/rules_engine/*'" in lines[at]
    at = _first(lines, "branch_coverage.py", at)
    assert "--min 90 --label rules_engine" in lines[at]
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "COV_FAIL_UNDER ?= 70" in makefile


def test_api_tests_runs_pytest_under_xdist(repo_root: Path) -> None:
    """api-tests spreads pytest over at most 4 xdist workers (`auto` by default), xdist_group
    tests stay on one worker, and coverage is still enforced over the combined data."""
    lines = _dry_run(repo_root, "api-tests")
    joined = " ".join(lines[_first(lines, "pytest -q") :])
    # `-n` may be overridden (`make api-tests API_TEST_WORKERS=1` reaches this sub-make).
    assert re.search(r" -n \S+ --maxprocesses=4 --dist loadgroup ", joined), joined
    assert "--cov=app" in joined and "--cov-fail-under=" in joined
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "API_TEST_WORKERS ?= auto" in makefile
    for target in ("tenant-isolation", "bootstrap-mimic"):
        assert " -n " not in _target(makefile, target), target


# ---- S12 T14: `make e2e` runs every scripts/e2e/*.sh, each after a limiter reset ----


def _e2e_recipe(repo_root: Path) -> str:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    found = re.search(r"(?ms)^e2e:\n((?:\t.*\n)+)", makefile)
    assert found is not None
    return found.group(1)


def test_e2e_target_runs_scripts_dir(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "E2E_DIR ?= scripts/e2e" in makefile
    assert "RESET_LIMITS ?= scripts/ci/reset_rate_limits.sh" in makefile
    assert ".PHONY: e2e" in makefile
    recipe = _e2e_recipe(repo_root)
    assert "$(E2E_DIR)/*.sh" in recipe
    assert "RATE_LIMIT" not in recipe


def _run_make_e2e(repo_root: Path, tmp_path: Path, fail_first: bool) -> list[str]:
    import os
    import subprocess

    log = tmp_path / "calls.log"
    scripts = tmp_path / "e2e"
    scripts.mkdir()
    for name in ("a", "b"):
        body = f'printf "%s\\n" "{name} $1" >> "{log}"\n'
        if name == "a" and fail_first:
            body += "exit 3\n"
        path = scripts / f"{name}.sh"
        path.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
        path.chmod(0o755)
    reset = tmp_path / "reset.sh"
    reset.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "reset $1" >> "{log}"\n', encoding="utf-8"
    )
    reset.chmod(0o755)
    fake = tmp_path / "fake.sh"
    fake.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "fake-$1 $2" >> "{log}"\n', encoding="utf-8"
    )
    fake.chmod(0o755)
    off = tmp_path / "off.sh"
    off.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "off-$1 $2" >> "{log}"\n', encoding="utf-8"
    )
    off.chmod(0o755)
    result = subprocess.run(
        [
            "make",
            "-s",
            "e2e",
            f"E2E_DIR={scripts}",
            f"RESET_LIMITS={reset}",
            f"FAKE_CTL={fake}",
            f"FLAG_OFF_CTL={off}",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, "API_HOST_PORT": "8123"},
        timeout=120,
        check=False,
    )
    assert (result.returncode != 0) is fail_first, result.stderr
    return [line.split(" ", 1)[0] for line in log.read_text(encoding="utf-8").splitlines()]


def test_make_e2e_resets_limits_before_each_script(repo_root: Path, tmp_path: Path) -> None:
    assert _run_make_e2e(repo_root, tmp_path, fail_first=False) == [
        "fake-start",
        "off-start",
        "reset",
        "a",
        "reset",
        "b",
        "off-stop",
        "fake-stop",
    ]


def test_make_e2e_stops_on_first_failure(repo_root: Path, tmp_path: Path) -> None:
    assert _run_make_e2e(repo_root, tmp_path, fail_first=True) == [
        "fake-start",
        "off-start",
        "reset",
        "a",
        "off-stop",
        "fake-stop",
    ]


def test_fake_marketplace_target_loopback(repo_root: Path) -> None:
    """S15: `make fake-marketplace` runs the test-support fake on its worktree port; the module
    binds 127.0.0.1 itself and the recipe never passes a host."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    recipe = _target(makefile, "fake-marketplace").strip()
    assert recipe == (
        "uv run --frozen python apps/api/tests/support/fake_marketplace.py "
        "--port $(FAKE_MARKETPLACE_HOST_PORT)"
    )
    assert "--host" not in recipe and "0.0.0.0" not in recipe  # noqa: S104
    source = (repo_root / "apps/api/tests/support/fake_marketplace.py").read_text(encoding="utf-8")
    assert 'host="127.0.0.1"' in source
    assert "0.0.0.0" not in source  # noqa: S104


def test_make_e2e_sets_fake_and_refresh_hooks(repo_root: Path) -> None:
    """S15: `make e2e` starts the fake for the pass (and stops it on any exit), and hands every
    script FAKE_URL and REFRESH_ONCE, the worker's one-shot refresh with the dev env."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "FAKE_CTL ?= scripts/ci/fake_marketplace.sh" in makefile
    assert (
        "REFRESH_ONCE ?= APP_ROLE=worker uv run --frozen python -m app.workers.oneshot "
        "refresh_marketplace_tokens" in makefile
    )
    recipe = _e2e_recipe(repo_root)
    start = recipe.index("$(FAKE_CTL) start $(FAKE_MARKETPLACE_HOST_PORT)")
    trap = recipe.index("trap '$(FAKE_CTL) stop $(FAKE_MARKETPLACE_HOST_PORT)' EXIT")
    hooks = recipe.index('export FAKE_URL="$(FAKE_MARKETPLACE_URL)" REFRESH_ONCE="$(REFRESH_ONCE)"')
    assert start < trap < hooks < recipe.index("for script in")
    assert "export $(FAKE_MARKETPLACE_ENV)" in recipe


def test_make_dev_points_marketplace_urls_at_fake(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "FAKE_MARKETPLACE_URL = http://127.0.0.1:$(FAKE_MARKETPLACE_HOST_PORT)" in makefile
    joined = makefile.replace("\\\n\t", " ")
    for name, path in (
        ("AMAZON_AUTHORIZE_URL", "amazon/authorize"),
        ("AMAZON_LWA_TOKEN_URL", "amazon/token"),
        ("FLIPKART_AUTHORIZE_URL", "flipkart/authorize"),
        ("FLIPKART_TOKEN_URL", "flipkart/token"),
    ):
        assert f"{name}=$(FAKE_MARKETPLACE_URL)/{path}" in joined
    # S21: the publish and status-sync jobs of the e2e worker talk SP-API to the fake only
    assert "AMAZON_SPAPI_ENDPOINT=$(FAKE_MARKETPLACE_URL)" in joined
    dev = _target(makefile, "dev")
    assert "$(FAKE_MARKETPLACE_ENV) uv run --frozen uvicorn app.main:app" in dev


def test_make_dev_flipkart_on_local_only(repo_root: Path) -> None:
    """S22 C2: `make dev` (and the e2e hooks) run Flipkart on against the fake's listing routes;
    the committed defaults keep it off everywhere else."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    joined = makefile.replace("\\\n\t", " ")
    assert "FLIPKART_API_BASE=$(FAKE_MARKETPLACE_URL)/flipkart/sellers" in joined
    assert "FLIPKART_ENABLED=true" in joined
    assert "$(FAKE_MARKETPLACE_ENV) uv run --frozen uvicorn app.main:app" in _target(
        makefile, "dev"
    )
    example = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "\nFLIPKART_ENABLED=false\n" in example
    config = (repo_root / "apps/api/app/core/config.py").read_text(encoding="utf-8")
    assert "flipkart_enabled: bool = False" in config


def test_make_e2e_starts_flag_off_api(repo_root: Path) -> None:
    """S22 C2: `make e2e` starts the flag-off API after the fake and its env, stops both on any
    exit, and hands the scripts FLAG_OFF_URL."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "FLAG_OFF_CTL ?= scripts/ci/flag_off_api.sh" in makefile
    recipe = _e2e_recipe(repo_root)
    env = recipe.index("export $(FAKE_MARKETPLACE_ENV)")
    start = recipe.index("$(FLAG_OFF_CTL) start $(FLAG_OFF_API_HOST_PORT)")
    trap = recipe.index(
        "trap '$(FLAG_OFF_CTL) stop $(FLAG_OFF_API_HOST_PORT); "
        "$(FAKE_CTL) stop $(FAKE_MARKETPLACE_HOST_PORT)' EXIT"
    )
    hook = recipe.index('export FLAG_OFF_URL="http://127.0.0.1:$(FLAG_OFF_API_HOST_PORT)"')
    assert env < start < trap < hook < recipe.index("for script in")
    script = (repo_root / "scripts/ci/flag_off_api.sh").read_text(encoding="utf-8")
    assert "FLIPKART_ENABLED=false nohup uv run --frozen python -m uvicorn app.main:app" in script
    assert "--host" not in script and "0.0.0.0" not in script  # noqa: S104
    assert "pkill" not in script and "killall" not in script
