"""ADR-0020: the Google sign-in settings. Google's endpoints are fixed outside local, the flag is
off by default, the api needs the client id and secret only while it is on, and the redirect URI
is derived from DASHBOARD_BASE_URL. Boot errors name variables only, never a value."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

DEPLOYED_URL = "postgresql+psycopg://u:p@10.0.0.1:5432/db"
DSN = "https://public@o0.ingest.example.invalid/0"
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"
CLIENT_ID = "1039991710726-74ijetqns5m41m2m3c6piso02j3vub2p.apps.googleusercontent.com"
CLIENT_SECRET = "test-only-google-client-secret-00000"
LOCAL_ENV = {
    "APP_ENV": "local",
    "DATABASE_URL": "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os",
    "JWT_SECRET": "ci_only_not_a_real_secret_at_least_32_chars",
}


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


def test_defaults_off_with_google_constants(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _boot(monkeypatch, LOCAL_ENV)
    assert settings.google_oauth_enabled is False  # type: ignore[attr-defined]
    assert settings.google_authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"  # type: ignore[attr-defined]
    assert settings.google_token_url == "https://oauth2.googleapis.com/token"  # type: ignore[attr-defined]
    assert settings.google_jwks_url == "https://www.googleapis.com/oauth2/v3/certs"  # type: ignore[attr-defined]
    assert settings.google_issuer == "https://accounts.google.com"  # type: ignore[attr-defined]
    assert settings.auth_google_rate_limit_per_min == 10  # type: ignore[attr-defined]


def test_redirect_uri_derived_from_dashboard_origin(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _boot(monkeypatch, {**LOCAL_ENV, "DASHBOARD_BASE_URL": "https://kuinn.in/"})
    assert settings.google_redirect_uri == "https://kuinn.in/api/v1/auth/google/callback"  # type: ignore[attr-defined]


def test_deployed_off_boots_without_client(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot(monkeypatch, _deployed(GOOGLE_OAUTH_ENABLED="false"))


def test_deployed_on_boots_with_client(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _boot(
        monkeypatch,
        _deployed(
            GOOGLE_OAUTH_ENABLED="true",
            GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID,
            GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        ),
    )
    assert settings.google_oauth_enabled is True  # type: ignore[attr-defined]
    assert CLIENT_SECRET not in repr(settings)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_on_requires_client_id_and_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    message = _abort(monkeypatch, _deployed(app_env, GOOGLE_OAUTH_ENABLED="true"))
    assert "GOOGLE_OAUTH_ENABLED=true requires GOOGLE_OAUTH_CLIENT_ID" in message
    assert "GOOGLE_OAUTH_ENABLED=true requires GOOGLE_OAUTH_CLIENT_SECRET" in message


def test_deployed_on_refuses_foreign_client_id(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _abort(
        monkeypatch,
        _deployed(
            GOOGLE_OAUTH_ENABLED="true",
            GOOGLE_OAUTH_CLIENT_ID="not-a-google-client",
            GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
        ),
    )
    assert "GOOGLE_OAUTH_CLIENT_ID must end with .apps.googleusercontent.com" in message
    assert "not-a-google-client" not in message


@pytest.mark.parametrize("secret", ["short-secret", "test-only-google has a space-0000000"])
def test_deployed_on_refuses_weak_secret(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    message = _abort(
        monkeypatch,
        _deployed(
            GOOGLE_OAUTH_ENABLED="true",
            GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID,
            GOOGLE_OAUTH_CLIENT_SECRET=secret,
        ),
    )
    assert "refuses a weak GOOGLE_OAUTH_CLIENT_SECRET" in message
    assert secret not in message


@pytest.mark.parametrize(
    "name", ["GOOGLE_AUTHORIZE_URL", "GOOGLE_TOKEN_URL", "GOOGLE_JWKS_URL", "GOOGLE_ISSUER"]
)
@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_refuses_endpoint_override(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str, name: str
) -> None:
    message = _abort(monkeypatch, _deployed(app_env, **{name: "https://evil.example/x"}))
    assert f"APP_ENV={app_env} refuses overriding {name}" in message
    assert "evil.example" not in message


def test_deployed_worker_refuses_override_too(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import _google_errors, get_settings

    _boot(monkeypatch, LOCAL_ENV)
    local = get_settings().model_copy(
        update={"app_env": "production", "app_role": "worker", "google_issuer": "https://x"}
    )
    assert _google_errors(local) == ["APP_ENV=production refuses overriding GOOGLE_ISSUER"]


def test_local_allows_fake_endpoints(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _boot(
        monkeypatch,
        {
            **LOCAL_ENV,
            "GOOGLE_OAUTH_ENABLED": "true",
            "GOOGLE_OAUTH_CLIENT_ID": "fake-client",
            "GOOGLE_OAUTH_CLIENT_SECRET": "fake-secret",
            "GOOGLE_AUTHORIZE_URL": "http://127.0.0.1:4020/google/authorize",
            "GOOGLE_ISSUER": "http://127.0.0.1:4020/google",
        },
    )
    assert settings.google_issuer == "http://127.0.0.1:4020/google"  # type: ignore[attr-defined]


def test_local_on_still_requires_client(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message = _abort(monkeypatch, {**LOCAL_ENV, "GOOGLE_OAUTH_ENABLED": "true"})
    assert "requires GOOGLE_OAUTH_CLIENT_ID" in message


def test_rate_limit_lower_bound(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message = _abort(monkeypatch, {**LOCAL_ENV, "AUTH_GOOGLE_RATE_LIMIT_PER_MIN": "0"})
    assert "AUTH_GOOGLE_RATE_LIMIT_PER_MIN" in message
