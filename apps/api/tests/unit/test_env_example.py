"""AC4: register parity between ENV.md, Settings and the root `.env.example` (CANON.md:64)."""

from __future__ import annotations

import shutil
from pathlib import Path

REMOVED = {
    "ENV",
    "REDIS_URL",
    "ARQ_QUEUE_NAME",
    "ARQ_MAX_JOBS",
    "ARQ_JOB_TIMEOUT_SECONDS",
    "NEXT_PUBLIC_API_BASE_URL",
    "NEXTAUTH_URL",
    "NEXTAUTH_SECRET",
}


def _env_example(repo_root: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"not a KEY=VALUE line: {raw!r}"
        pairs.append((key, value))
    return pairs


def test_settings_fields_equal_register_names(register_names: list[str]) -> None:
    from app.core.config import Settings

    fields = [name.upper() for name in Settings.model_fields]
    assert len(register_names) == len(set(register_names))
    assert set(fields) == set(register_names)


def test_env_example_keys_in_register_order(repo_root: Path, register_names: list[str]) -> None:
    assert [key for key, _ in _env_example(repo_root)] == register_names


def test_env_example_has_no_duplicate_keys(repo_root: Path) -> None:
    keys = [key for key, _ in _env_example(repo_root)]
    assert len(keys) == len(set(keys))


def test_cp_env_example_boots_api_role(repo_root: Path, clean_env: Path) -> None:
    from app.core.config import get_settings

    shutil.copyfile(repo_root / ".env.example", clean_env / ".env")
    loaded = get_settings()
    assert loaded.app_env == "local"
    assert loaded.app_role == "api"


def test_removed_rows_absent(repo_root: Path, register_names: list[str]) -> None:
    from app.core.config import Settings

    keys = {key for key, _ in _env_example(repo_root)}
    fields = {name.upper() for name in Settings.model_fields}
    for source in (set(register_names), keys, fields):
        assert not source & REMOVED
        assert not {name for name in source if name.startswith("ARQ_")}


def test_database_url_is_canon_local(repo_root: Path) -> None:
    values = dict(_env_example(repo_root))
    local = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
    assert values["DATABASE_URL"] == local
    assert values["ALEMBIC_DATABASE_URL"] == local


def test_bucket_names_and_blank_gcp_project(repo_root: Path) -> None:
    values = dict(_env_example(repo_root))
    assert values["GCS_BUCKET_IMAGES"] == "marketplace-images"
    assert values["GCS_BUCKET_CSV"] == "marketplace-csv"
    assert values["GCP_PROJECT_ID"] == ""
