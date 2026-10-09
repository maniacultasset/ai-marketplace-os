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


# ---- S20 T16 (AC16; D10; C17): the SP-API host pinned to the sandbox flag ----

SANDBOX = "https://sandbox.sellingpartnerapi-eu.amazon.com"
PROD = "https://sellingpartnerapi-eu.amazon.com"


def _spapi(app_env: str, role: str = "api", **over: str) -> dict[str, str]:
    env = _env(app_env, role)
    env.update(over)
    return env


def _refused(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    return info.value.code


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_boot_spapi_https_off_local(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    code = _refused(
        monkeypatch,
        _spapi(app_env, AMAZON_SPAPI_ENDPOINT="http://sandbox.sellingpartnerapi-eu.amazon.com"),
    )
    assert "AMAZON_SPAPI_ENDPOINT must be a bare https origin" in code


def test_boot_spapi_sandbox_requires_sandbox_host(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _spapi("staging", AMAZON_SPAPI_ENDPOINT=SANDBOX, AMAZON_USE_SANDBOX="true")
    assert _boot(monkeypatch, **env) is not None


def test_boot_spapi_prod_requires_prod_host(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _spapi("production", AMAZON_SPAPI_ENDPOINT=PROD, AMAZON_USE_SANDBOX="false")
    assert _boot(monkeypatch, **env) is not None


@pytest.mark.parametrize("role", ["api", "worker"])
def test_boot_spapi_prod_host_with_sandbox_true_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    env = _spapi("staging", role, AMAZON_SPAPI_ENDPOINT=PROD, AMAZON_USE_SANDBOX="true")
    code = _refused(monkeypatch, env)
    assert "must be the SP-API sandbox host while AMAZON_USE_SANDBOX=true" in code


def test_boot_spapi_sandbox_host_with_flag_false_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _spapi("production", AMAZON_SPAPI_ENDPOINT=SANDBOX, AMAZON_USE_SANDBOX="false")
    assert "production host while AMAZON_USE_SANDBOX=false" in _refused(monkeypatch, env)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_boot_spapi_unknown_https_host_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    env = _spapi(app_env, AMAZON_SPAPI_ENDPOINT="https://sellingpartnerapi.evil.test")
    assert "AMAZON_SPAPI_ENDPOINT" in _refused(monkeypatch, env)


def test_boot_spapi_path_port_userinfo_query_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in (SANDBOX + ":444", SANDBOX + "/v0", "https://u:p@sandbox.sellingpartnerapi-eu.amazon.com", SANDBOX + "?a=b"):  # fmt: skip
        assert "bare https origin" in _refused(
            monkeypatch, _spapi("staging", AMAZON_SPAPI_ENDPOINT=bad)
        )


def test_boot_spapi_loopback_http_ok_on_local(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"APP_ENV": "local", "APP_ROLE": "worker", "DATABASE_URL": LOCAL_URL}
    assert _boot(monkeypatch, **env, AMAZON_SPAPI_ENDPOINT="http://127.0.0.1:4020") is not None


def test_boot_spapi_local_keeps_the_committed_defaults(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.env.example` pair (prod host, sandbox true) still boots on local; `open_adapter`
    refuses it there before any call (T15)."""
    env = {"APP_ENV": "local", "APP_ROLE": "api", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": PRIVATE_JWT}  # fmt: skip
    assert _boot(monkeypatch, **env, AMAZON_SPAPI_ENDPOINT=PROD, AMAZON_USE_SANDBOX="true")


def test_boot_spapi_loopback_refused_off_local(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _spapi("staging", AMAZON_SPAPI_ENDPOINT="http://127.0.0.1:4020")
    assert "AMAZON_SPAPI_ENDPOINT" in _refused(monkeypatch, env)


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_boot_spapi_sandbox_false_only_in_production(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    env = (
        {"APP_ENV": "local", "APP_ROLE": "shell"}
        if app_env == "local"
        else _spapi(app_env, AMAZON_SPAPI_ENDPOINT=PROD)
    )
    code = _refused(monkeypatch, {**env, "AMAZON_USE_SANDBOX": "false"})
    assert f"APP_ENV={app_env} refuses AMAZON_USE_SANDBOX=false" in code


def test_boot_spapi_message_names_env_vars_only(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _spapi("staging", AMAZON_SPAPI_ENDPOINT="https://leaky-host-zq9.evil.test")
    code = _refused(monkeypatch, env)
    assert "leaky-host-zq9" not in code and "evil" not in code


def test_boot_spapi_migrate_role_not_checked(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the roles that reach SP-API (api, worker) are pinned; a migrate job is not."""
    env = _spapi("staging", "migrate", AMAZON_SPAPI_ENDPOINT=PROD, ALEMBIC_DATABASE_URL=LOCAL_URL)
    assert _boot(monkeypatch, **env) is not None
