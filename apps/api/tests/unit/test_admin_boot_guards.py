"""S14b T16 (AC 10; D10, ENV.md:22-27): the admin boot guards. Messages name variables only."""

from __future__ import annotations

from pathlib import Path

import pytest

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"
PRIVATE_ADMIN = "Zq9-admin-session-not-committed-32+"


def _env(app_env: str = "staging", **over: str) -> dict[str, str]:
    env = {
        "APP_ENV": app_env,
        "APP_ROLE": "api",
        "DATABASE_URL": LOCAL_URL,
        "JWT_SECRET": PRIVATE_JWT,
        "SENTRY_DSN": DSN,
        "ADMIN_ENABLED": "true",
        "ADMIN_SESSION_SECRET": PRIVATE_ADMIN,
    }
    env.update(over)
    return env


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


@pytest.mark.parametrize(
    "secret",
    [
        "dev_only_admin_session_secret_at_least_32_chars",
        "ci_only_admin_session_secret_at_least_32_chars",
        "local_only_admin_session_secret_32_characters",
        "  dev_only_admin_session_secret_at_least_32_chars  ",
        "DEV_ONLY_Admin_Session_Secret_At_Least_32_Chars",
    ],
    ids=["dev_only", "ci_only", "local_only", "padded", "recased"],
)
def test_admin_boot_guards_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    message = _abort(monkeypatch, _env(ADMIN_SESSION_SECRET=secret))
    assert "APP_ENV=staging refuses a committed or dev-prefixed ADMIN_SESSION_SECRET" in message
    assert secret.strip() not in message


def test_admin_boot_guards_secret_ok_local(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env("local", ADMIN_SESSION_SECRET="dev_only_admin_session_secret_at_least_32_chars")
    assert _boot(monkeypatch, env) is not None


def test_admin_boot_guards_secret_ignored_when_disabled(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(ADMIN_ENABLED="false", ADMIN_SESSION_SECRET="dev_only_admin_session_secret_32_c")
    assert _boot(monkeypatch, env) is not None


def test_admin_boot_guards_staging_private_secret_ok(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _boot(monkeypatch, _env()) is not None


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_admin_boot_guards_read_only_false_exits(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    message = _abort(monkeypatch, _env(app_env, ADMIN_READ_ONLY="false"))
    assert "ADMIN_READ_ONLY=false is refused in v1" in message


@pytest.mark.parametrize("url", ["/admin", "/internal/admin/", "/internal/ops"])
def test_admin_boot_guards_base_url_exits(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    message = _abort(monkeypatch, _env("local", ADMIN_BASE_URL=url))
    assert "ADMIN_BASE_URL must be /internal/admin" in message
    assert url not in message.replace("/internal/admin", "")


def test_admin_boot_guards_message_has_no_value(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "dev_only_Zq9_unique_value_in_the_secret_0042"
    message = _abort(
        monkeypatch,
        _env(
            "production",
            ADMIN_SESSION_SECRET=secret,
            ADMIN_READ_ONLY="false",
            ADMIN_BASE_URL="/x-unique-0042",
        ),
    )
    assert "0042" not in message
    for part in ("ADMIN_ENABLED", "ADMIN_SESSION_SECRET", "ADMIN_READ_ONLY", "ADMIN_BASE_URL"):
        assert part in message
