"""S3 T16 (AC10): the OpenAPI export is deterministic, versioned, env-independent and DB-free."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from tests.conftest import CI_DATABASE_URL, CI_JWT_SECRET, settings_env_names

REPO = Path(__file__).resolve().parents[4]


def _env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in set(settings_env_names())}
    env.update({"APP_ENV": "local", "DATABASE_URL": CI_DATABASE_URL, "JWT_SECRET": CI_JWT_SECRET})
    env.update(overrides)
    return env


def _export(tmp_path: Path, name: str, **overrides: str) -> bytes:
    out = tmp_path / name
    subprocess.run(
        [sys.executable, "-m", "app.openapi_export", "--out", str(out)],
        cwd=tmp_path,
        env=_env(**overrides),
        check=True,
        capture_output=True,
        timeout=120,
    )
    return out.read_bytes()


def test_openapi_export_deterministic(tmp_path: Path) -> None:
    first = _export(tmp_path, "a.json", PYTHONHASHSEED="1")
    second = _export(tmp_path, "b.json", PYTHONHASHSEED="4242")
    assert first == second
    assert first.endswith(b"}\n")
    spec = json.loads(first)
    assert "servers" not in spec
    assert list(spec) == sorted(spec)


def test_info_version_is_schemas_version(tmp_path: Path) -> None:
    spec = json.loads(_export(tmp_path, "v.json"))
    assert spec["info"]["version"] == metadata.version("mkos-schemas") == "1.0.0"


def test_export_ignores_app_env_production(tmp_path: Path) -> None:
    local = _export(tmp_path, "local.json")
    production = _export(
        tmp_path,
        "prod.json",
        APP_ENV="production",
        JWT_SECRET="a-production-shaped-secret-that-is-not-committed-0123",
        SENTRY_DSN="https://public@o0.ingest.example.invalid/0",
        KMS_KEY_URI=(
            "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/"
            "oauth-token-key"
        ),
        DASHBOARD_BASE_URL="https://app.example.invalid",
    )
    assert local == production


def test_export_needs_no_db(tmp_path: Path) -> None:
    closed = "postgresql+psycopg://postgres:postgres@127.0.0.1:1/nothing"
    assert _export(tmp_path, "nodb.json", DATABASE_URL=closed) == _export(tmp_path, "db.json")


def test_committed_openapi_matches_export(tmp_path: Path) -> None:
    committed = REPO / "packages/schemas/jsonschema/openapi.json"
    if not committed.is_file():
        pytest.fail(
            "packages/schemas/jsonschema/openapi.json is missing: run make -C packages/schemas all"
        )
    assert committed.read_bytes() == _export(tmp_path, "now.json")


@pytest.mark.parametrize(
    "relative",
    ["scripts/gen_ts_types.sh", "packages/schemas/openapi.json", "packages/schemas/generated"],
)
def test_superseded_paths_absent(relative: str) -> None:
    # SUPERSEDED.md:46: generated output lives only at the Canon paths.
    assert not (REPO / relative).exists()
