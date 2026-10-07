"""S14b T14 (AC 5; CD2): a production worker refuses to boot with the pipeline stub installed. The
API, migrate and shell roles never run the pipeline, and the check never imports it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"
REFUSAL = "APP_ENV=production APP_ROLE=worker refuses mkos-pipeline-stub"


def _boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    from app.core.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def _env(app_env: str, role: str) -> dict[str, str]:
    env = {
        "APP_ENV": app_env,
        "APP_ROLE": role,
        "DATABASE_URL": LOCAL_URL,
        "SENTRY_DSN": DSN,
        **DEPLOYED_S15_ENV,
    }
    if role == "api":
        env["JWT_SECRET"] = PRIVATE_JWT
    return env


def test_boot_refuses_stub_in_production(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **_env("production", "worker"))
    assert isinstance(info.value.code, str) and REFUSAL in info.value.code


@pytest.mark.parametrize("role", ["api", "migrate", "shell"])
def test_boot_allows_stub_api_production(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    env = _env("production", role)
    if role == "migrate":
        env["ALEMBIC_DATABASE_URL"] = LOCAL_URL
    assert _boot(monkeypatch, **env) is not None


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_boot_allows_stub_worker_staging(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    assert _boot(monkeypatch, **_env(app_env, "worker")) is not None


def test_boot_stub_check_without_import(clean_env: Path) -> None:
    """In a fresh interpreter: the check reads the distribution metadata and never imports the
    pipeline module (this shared pytest process imported it already)."""
    code = (
        "import sys\n"
        "from app.core import config\n"
        "try:\n"
        "    config.get_settings()\n"
        "except SystemExit as exc:\n"
        "    print('EXIT', exc.code)\n"
        "print('IMPORTED', 'mkos_pipeline' in sys.modules)\n"
    )
    keep = {
        k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    env = {**keep, **_env("production", "worker")}
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=clean_env,
        env={**env, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORTED False" in result.stdout
    assert REFUSAL in result.stdout
    assert json.dumps(REFUSAL)  # the message names variables only
