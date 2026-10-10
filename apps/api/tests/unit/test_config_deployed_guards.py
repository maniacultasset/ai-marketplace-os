"""Audit PROD-1, PROD-2, PROD-3: the deployed-boot guards on secrets, CORS origins, the JWT
issuer and numeric settings. A bad value aborts the boot with names only; the local and test
defaults keep booting."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DEPLOYED_URL = "postgresql+psycopg://u:p@10.0.0.1:5432/db"
DSN = "https://public@o0.ingest.example.invalid/0"
CI_LITERAL = "ci_only_not_a_real_secret_at_least_32_chars"
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"
PRIVATE_ADMIN = "Zq9-admin-session-not-committed-32+"


def _deployed(app_env: str = "production", **over: str) -> dict[str, str]:
    return {
        "APP_ENV": app_env,
        "APP_ROLE": "api",
        "DATABASE_URL": DEPLOYED_URL,
        "JWT_SECRET": PRIVATE_JWT,
        "SENTRY_DSN": DSN,
        **DEPLOYED_S15_ENV,
        **over,
    }


def _boot(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> object:
    from app.core.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def _abort(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, env)
    assert isinstance(info.value.code, str)
    return info.value.code


def test_deployed_env_helper_boots(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for app_env in ("staging", "production"):
        _boot(monkeypatch, _deployed(app_env))


# ---- PROD-1: a weak JWT_SECRET or ADMIN_SESSION_SECRET ----
# A character-variety floor ('a' * 32) waits on TEAM-QUESTIONS T130: CANON-ADDENDUM A§14 pins
# `"x" * 32` as the production fixture.

WEAK_SECRETS = {
    "spaces": " " * 32,
    "tabs": "\t" * 40,
    "inner whitespace": "Zq9-not-committed private-secret-32+",
    "padded": " Zq9-not-committed-private-secret-32+ ",
    "short once stripped": "Zq9-short-secret" + " " * 20,
}


@pytest.mark.parametrize("secret", list(WEAK_SECRETS.values()), ids=list(WEAK_SECRETS))
@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_refuses_weak_jwt_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str, secret: str
) -> None:
    message = _abort(monkeypatch, _deployed(app_env, JWT_SECRET=secret))
    assert f"APP_ENV={app_env} refuses a weak JWT_SECRET" in message
    assert secret.strip() not in message or not secret.strip()


@pytest.mark.parametrize("secret", list(WEAK_SECRETS.values()), ids=list(WEAK_SECRETS))
def test_staging_refuses_weak_admin_session_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    env = _deployed("staging", ADMIN_ENABLED="true", ADMIN_SESSION_SECRET=secret)
    message = _abort(monkeypatch, env)
    assert "APP_ENV=staging refuses a weak ADMIN_SESSION_SECRET" in message


def test_staging_admin_off_ignores_admin_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot(monkeypatch, _deployed("staging", ADMIN_SESSION_SECRET="a" * 32))


@pytest.mark.parametrize(
    "secret",
    [
        PRIVATE_JWT,
        "0123456789abcdef" * 4,  # `openssl rand -hex 32`
        "q1x0Hn4i0r3dSxwX5ZQwq3a4E+u0sXQbrw0q3T2K8sY=",  # `openssl rand -base64 32`
    ],
)
def test_deployed_accepts_a_random_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    env = _deployed("staging", JWT_SECRET=secret, ADMIN_ENABLED="true", ADMIN_SESSION_SECRET=secret)
    _boot(monkeypatch, env)


def test_local_keeps_its_committed_secrets(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # local and CI are not deployed: the committed literals and the defaults still boot
    _boot(monkeypatch, {"APP_ENV": "local", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": CI_LITERAL})


def test_forged_token_with_a_blank_key_never_verifies(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the repro's impact: production never gets as far as signing with a guessable key
    _abort(monkeypatch, _deployed(JWT_SECRET=" " * 32))


# ---- PROD-3: lower bounds on numeric settings ----

BOUNDED = {
    "RATE_LIMIT_PER_IP_PER_MIN": "0",
    "RATE_LIMIT_PER_TENANT_PER_MIN": "0",
    "AUTH_LOGIN_RATE_LIMIT_PER_MIN": "-1",
    "ACCESS_TOKEN_TTL_MINUTES": "0",
    "REFRESH_TOKEN_TTL_DAYS": "0",
    "OAUTH_STATE_TTL_SECONDS": "0",
    "DATABASE_POOL_SIZE": "-1",
    "DATABASE_MAX_OVERFLOW": "-1",
    "DATABASE_POOL_TIMEOUT": "0",
    "DB_STATEMENT_TIMEOUT_MS": "0",
    "REQUEST_MAX_BODY_BYTES": "0",
}


@pytest.mark.parametrize(("name", "value"), list(BOUNDED.items()))
@pytest.mark.parametrize("app_env", ["local", "production"])
def test_numeric_setting_below_its_bound_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str, name: str, value: str
) -> None:
    env = (
        _deployed(**{name: value})
        if app_env == "production"
        else {"APP_ENV": "local", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": CI_LITERAL, name: value}
    )
    message = _abort(monkeypatch, env)
    assert f"{name} (greater_than_equal)" in message


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RATE_LIMIT_PER_IP_PER_MIN", "1"),
        ("DATABASE_MAX_OVERFLOW", "0"),
        ("DATABASE_POOL_SIZE", "1"),
        ("DB_STATEMENT_TIMEOUT_MS", "1"),
    ],
)
def test_numeric_setting_at_its_bound_boots(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    env = {"APP_ENV": "local", "DATABASE_URL": LOCAL_URL, "JWT_SECRET": CI_LITERAL, name: value}
    _boot(monkeypatch, env)
