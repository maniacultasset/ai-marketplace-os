"""AC18: Renovate, CODEOWNERS, the PR template and the contracts script
(EXECUTION-PLAN.md:162-163, :200-216, :264; CANON.md:80; ADR-0013 §6)."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

BRANCH_RE = r"^(feat|fix|chore|docs|refactor|test|perf)/[a-z0-9._-]+$"
CODEOWNERS = [
    "*                      @maniacultasset/tech-lead",
    "/apps/api/             @maniacultasset/backend @maniacultasset/tech-lead",
    "/packages/schemas/     @maniacultasset/tech-lead @maniacultasset/backend"
    " @maniacultasset/ai-eng @maniacultasset/frontend",
    "/infra/                @maniacultasset/devops-qa @maniacultasset/tech-lead",
    "/apps/api/alembic/     @maniacultasset/tech-lead",
    "/scripts/db/           @maniacultasset/tech-lead",
    "/.github/              @maniacultasset/tech-lead",
    "/.gitleaks.toml        @maniacultasset/tech-lead",
    "/.importlinter         @maniacultasset/tech-lead",
    "/pyproject.toml        @maniacultasset/tech-lead",
    "/docs/adr/             @maniacultasset/tech-lead",
]
TEN_CHECKS = (
    "api-lint",
    "api-typecheck",
    "api-tests",
    "tenant-isolation",
    "contracts",
    "schemas-drift",
    "commit-lint",
    "bootstrap",
    "lint-imports",
    "gitleaks",
)


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _renovate(repo_root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((repo_root / "renovate.json").read_text(encoding="utf-8"))
    return data


def _codeowners(repo_root: Path) -> list[str]:
    text = (repo_root / ".github/CODEOWNERS").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_renovate_never_automerges(repo_root: Path) -> None:
    config = _renovate(repo_root)
    assert config["automerge"] is False
    for key, value in _walk(config):
        if key.lower().startswith("automerge") and key != "automergeType":
            assert value is False, key
    assert not (repo_root / "renovate.yml").exists()


def test_renovate_branch_prefix_passes_branch_regex(repo_root: Path) -> None:
    config = _renovate(repo_root)
    assert config["branchPrefix"] == "chore/renovate-"
    assert re.fullmatch(BRANCH_RE, config["branchPrefix"] + "fastapi-0.x")
    assert config["semanticCommits"] == "enabled"
    assert config["semanticCommitType"] == "chore"
    assert config["semanticCommitScope"] == "deps"
    assert config["pinDigests"] is True


def test_single_codeowners_file(repo_root: Path) -> None:
    # GitHub reads CODEOWNERS from exactly these three places; only one may exist.
    places = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
    assert [p for p in places if (repo_root / p).exists()] == [".github/CODEOWNERS"]


def test_codeowners_lines_exact(repo_root: Path) -> None:
    lines = _codeowners(repo_root)
    assert lines == CODEOWNERS
    assert not [line for line in lines if re.search(r"apps/web|packages/pipeline|AGENTS\.md", line)]


def test_codeowners_team_handles_only(repo_root: Path) -> None:
    for line in _codeowners(repo_root):
        pattern, *owners = line.split()
        assert owners, pattern
        for owner in owners:
            assert re.fullmatch(r"@maniacultasset/[a-z0-9-]+", owner), owner


def test_pr_template_lists_ten_checks(repo_root: Path) -> None:
    template = (repo_root / ".github/pull_request_template.md").read_text(encoding="utf-8")
    for check in TEN_CHECKS:
        assert f"`{check}`" in template, check
    for heading in ("Summary", "Done when", "Checks", "ADRs", "Contract impact", "Risk"):
        assert heading in template, heading


def test_worktree_env_is_ignored(repo_root: Path) -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".worktree.env"],
        cwd=repo_root,
        check=False,
    )
    assert ignored.returncode == 0
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env.example"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0
    sample = subprocess.run(
        ["git", "check-ignore", "-q", "worktree.env.sample"],
        cwd=repo_root,
        check=False,
    )
    assert sample.returncode == 1


def test_contracts_script_installs_only_hashed_deps(repo_root: Path) -> None:
    script = (repo_root / "scripts/ci/contracts.sh").read_text(encoding="utf-8")
    assert "--seed" not in script
    assert "uv export --frozen --package mkos-schemas" in script
    assert "--require-hashes -r build-constraints.txt" in script
    assert '--require-hashes -r "$work/runtime.txt"' in script
    assert "--no-build-isolation --no-index -e packages/schemas" in script
    assert "from mkos_schemas.listing import CanonicalListing" in script
    assert "contracts_check.py --fixtures packages/schemas/fixtures" in script
    assert "pip install" not in script.replace(
        'uv pip install --python "$venv/bin/python" --require-hashes -r build-constraints.txt', ""
    ).replace(
        'uv pip install --python "$venv/bin/python" --require-hashes -r "$work/runtime.txt"', ""
    ).replace(
        '"$venv/bin/pip" install --no-build-isolation --no-index -e packages/schemas', ""
    )


def test_mypy_excludes_alembic_versions(repo_root: Path) -> None:
    # CANON.md:135: revision files are generated; mypy strict skips them.
    import tomllib

    mypy = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]
    # S26 T23: and mutmut's git-ignored working copy (`make mutation`)
    assert mypy["exclude"] == ["^apps/api/alembic/versions/", "^apps/api/mutants/"]


def test_mutmut_scoped_to_core_modules(repo_root: Path) -> None:
    """S26 T23 (PLAN D9): the core modules and their own tests; every path exists."""
    import tomllib

    config = tomllib.loads((repo_root / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    mutmut = config["tool"]["mutmut"]
    assert mutmut["source_paths"] == ["app"]
    assert mutmut["only_mutate"] == [
        "app/services/rules_engine/*",
        "app/db/guard.py",
        "app/repositories/base.py",
        "app/services/maker_checker.py",
        "app/services/idempotency.py",
        "app/repositories/idempotency.py",
        "app/services/publish_rules.py",
        "app/services/publish_ledger.py",
        "app/services/publish.py",
        # DPP-1: the pure security modules of the sign-in
        "app/core/totp.py",
        "app/core/recovery_codes.py",
        "app/core/password_policy.py",
        "app/services/sign_in_policy.py",
    ]
    api = repo_root / "apps/api"
    for entry in mutmut["only_mutate"]:
        matches = list(api.glob(entry))
        assert matches and all(p.suffix == ".py" or p.is_dir() for p in matches), entry
    selection = mutmut["pytest_add_cli_args_test_selection"]
    assert selection[0] == "tests/unit/rules_engine"
    for path in selection:
        assert (api / path).exists(), path
        assert path.startswith(("tests/unit", "tests/integration")), path
    assert mutmut["pytest_add_cli_args"] == [
        "-c",
        "pyproject.toml",
        "--rootdir",
        ".",
        "--import-mode=importlib",
        "--no-cov",
        "-p",
        "no:randomly",
        "-o",
        "asyncio_mode=auto",
    ]
    assert mutmut["also_copy"] == ["tests", "alembic", "alembic.ini"]
    root = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mutmut" not in root["tool"]
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "uv run --frozen mutmut run --max-children $(MUTATION_MAX_CHILDREN)" in makefile
    # Parallel children, each on its own slot copy of mos_test, on a fresh mutants copy.
    assert "MUTATION_MAX_CHILDREN ?= 6" in makefile
    assert "\trm -rf apps/api/mutants\n" in makefile
    assert makefile.count("tests/support/xdist_db.py drop-mutant-databases") == 2
    assert (
        "uv run --frozen python scripts/ci/mutation_report.py --root apps/api "
        "--waivers docs/review/MUTATION-WAIVERS.md" in makefile
    )
    assert "\nmutation-rules: mutation\n" in makefile
    assert (repo_root / "docs/review/MUTATION-WAIVERS.md").is_file()
    assert "mutants/" in (repo_root / ".gitignore").read_text(encoding="utf-8")
