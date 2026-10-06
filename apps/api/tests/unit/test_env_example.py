"""AC4: register parity between ENV.md, Settings and the root `.env.example` (CANON.md:64)."""

from __future__ import annotations

import re
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


_APP_URL = (
    "postgresql+psycopg://app_rw:ci_only_app_rw_not_a_real_password@localhost:5432/marketplace_os"
)
_MIGRATOR_URL = (
    "postgresql+psycopg://migrator:ci_only_migrator_not_a_real_password@localhost:5432/"
    "marketplace_os"
)


def _env_md_locals(repo_root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in (repo_root / "docs" / "canon" / "ENV.md").read_text(encoding="utf-8").splitlines():
        match = _ENV_LOCAL.match(raw)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_database_url_is_canon_local(repo_root: Path) -> None:
    # ENV.md:34-35 (S10, user OK 2026-10-06): the app as app_rw, Alembic as migrator, psycopg 3
    values = dict(_env_example(repo_root))
    canon = _env_md_locals(repo_root)
    assert values["DATABASE_URL"] == canon["DATABASE_URL"] == _APP_URL
    assert values["ALEMBIC_DATABASE_URL"] == canon["ALEMBIC_DATABASE_URL"] == _MIGRATOR_URL
    assert "asyncpg" not in canon["DATABASE_URL"] + canon["ALEMBIC_DATABASE_URL"]


def test_env_example_app_is_app_rw(repo_root: Path) -> None:
    """CODE-PLAN S10 Q5: a superuser ignores RLS even under FORCE, so the local app must not log
    in as one; the passwords are the Makefile's local dev-only constants bootstrap-db sets."""
    from urllib.parse import urlsplit

    values = dict(_env_example(repo_root))
    app, migrator = urlsplit(values["DATABASE_URL"]), urlsplit(values["ALEMBIC_DATABASE_URL"])
    assert (app.username, migrator.username) == ("app_rw", "migrator")
    assert app.username != "postgres" and migrator.username != "postgres"
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert f"\nCI_APP_RW_PASSWORD := {app.password}\n" in makefile
    assert f"\nCI_MIGRATOR_PASSWORD := {migrator.password}\n" in makefile
    for url in (app, migrator):
        assert str(url.password).startswith("ci_only_")
        assert (url.hostname, url.port, url.path) == ("localhost", 5432, "/marketplace_os")


_LOCAL_ONLY = re.compile(r"^local_only_[A-Za-z0-9_]+$")
_Q1_ACCESS = "local_only_minio_root_user"
_Q1_SECRET = "local_only_minio_root_password_32_chars"
_ENV_LOCAL = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \| `([^`]*)` \|")


def test_s3_credentials_are_local_only_values(repo_root: Path) -> None:
    values = dict(_env_example(repo_root))
    access, secret = values["S3_ACCESS_KEY"], values["S3_SECRET_KEY"]
    assert access == _Q1_ACCESS
    assert secret == _Q1_SECRET
    assert _LOCAL_ONLY.fullmatch(access)
    assert _LOCAL_ONLY.fullmatch(secret)
    assert len(secret) >= 8
    assert access != "minioadmin"
    assert secret != "minioadmin"


def test_s3_values_match_env_md_and_config(repo_root: Path) -> None:
    from app.core.config import Settings

    example = dict(_env_example(repo_root))
    locals_by_name: dict[str, str] = {}
    for raw in (repo_root / "docs" / "canon" / "ENV.md").read_text(encoding="utf-8").splitlines():
        match = _ENV_LOCAL.match(raw)
        if match:
            locals_by_name[match.group(1)] = match.group(2)
    for key, field in (
        ("S3_ACCESS_KEY", "s3_access_key"),
        ("S3_SECRET_KEY", "s3_secret_key"),
    ):
        assert locals_by_name[key] == example[key]
        assert Settings.model_fields[field].default == example[key]


def test_bucket_names_and_blank_gcp_project(repo_root: Path) -> None:
    values = dict(_env_example(repo_root))
    assert values["GCS_BUCKET_IMAGES"] == "marketplace-images"
    assert values["GCS_BUCKET_CSV"] == "marketplace-csv"
    assert values["GCP_PROJECT_ID"] == ""
