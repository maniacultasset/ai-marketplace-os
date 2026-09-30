"""AC1: the backend-only tree (ADR-0012:20-21; SUPERSEDED.md:37)."""

from __future__ import annotations

from pathlib import Path

REQUIRED_DIRS = (
    "apps/api/app",
    "apps/api/tests",
    "packages/schemas/src/mkos_schemas",
    "packages/schemas/jsonschema",
    "packages/schemas/ts/src",
    "infra",
    "scripts/db",
    "scripts/ci",
    ".github/workflows",
)
APP_SUBPACKAGES = (
    "",
    "core",
    "api",
    "api/v1",
    "api/v1/routers",
    "services",
    "repositories",
    "workers",
    "adapters",
    "db",
)
FORBIDDEN_DIRS = ("apps/web", "packages/pipeline", "apps/api/app/agents")


def test_required_dirs_exist(repo_root: Path) -> None:
    missing = [d for d in REQUIRED_DIRS if not (repo_root / d).is_dir()]
    assert not missing


def test_app_subpackages_have_init(repo_root: Path) -> None:
    app = repo_root / "apps/api/app"
    missing = [p for p in APP_SUBPACKAGES if not (app / p / "__init__.py").is_file()]
    assert not missing


def test_forbidden_dirs_absent(repo_root: Path) -> None:
    present = [d for d in FORBIDDEN_DIRS if (repo_root / d).exists()]
    assert not present


def test_scaffold_moves(repo_root: Path) -> None:
    moved = (
        "scaffold/apps/api/app/core/config.py",
        "scaffold/apps/api/Dockerfile",
        "scaffold/apps/api/docker-entrypoint.sh",
        "scaffold/.dockerignore",
    )
    assert not [path for path in moved if (repo_root / path).exists()]
    assert (repo_root / "scaffold/README.md").is_file()  # SUPERSEDED.md:103: never moved
