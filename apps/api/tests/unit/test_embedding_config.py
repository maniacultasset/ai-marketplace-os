"""S16 T2 (AC7; PLAN D4, D5; R2): the openai pin, the price table and the worker boot rule."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.model_prices import embedding_cost_usd, is_usable_openai_key
from tests.conftest import REPO_ROOT
from tests.support.deployed_env import DEPLOYED_S15_ENV

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"
CANARY_KEY = "canary-key-value-7f3e9a"
NO_KEY = "APP_ROLE=worker requires OPENAI_API_KEY outside APP_ENV=local"
NO_PRICE = "EMBEDDING_MODEL has no price entry"


def _boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    from app.core.config import get_settings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


def _env(app_env: str, role: str, **extra: str) -> dict[str, str]:
    env = {"APP_ENV": app_env, "APP_ROLE": role, "DATABASE_URL": LOCAL_URL}
    if app_env != "local":
        env |= {"SENTRY_DSN": DSN, **DEPLOYED_S15_ENV}
        env.pop("OPENAI_API_KEY")
    if role == "api":
        env["JWT_SECRET"] = "Zq9-not-committed-private-secret-32+"
    return env | extra


def _refusal(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    return info.value.code


def test_lock_pins_openai_exact() -> None:
    project = tomllib.loads((REPO_ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    pins = [d for d in project["project"]["dependencies"] if d.startswith("openai")]
    assert pins == ["openai==3.19.1"]
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == "openai"]
    assert versions == ["3.19.1"]


def test_cost_vector() -> None:
    model = "text-embedding-3-small"
    assert embedding_cost_usd(model, 1_000_000) == Decimal("0.020000")
    assert str(embedding_cost_usd(model, 1_000_000)) == "0.020000"
    assert embedding_cost_usd(model, 123) == Decimal("0.000002")
    assert embedding_cost_usd(model, 25) == Decimal("0.000001")  # 0.0000005 rounds half up
    assert str(embedding_cost_usd(model, 0)) == "0.000000"


def test_cost_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        embedding_cost_usd("text-embedding-3-large", 10)
    with pytest.raises(ValueError):
        embedding_cost_usd("text-embedding-3-small", -1)


@pytest.mark.parametrize(
    ("key", "usable"),
    [(None, False), ("", False), ("   ", False), ("sk-REPLACE_ME", False), ("sk-real", True)],
)
def test_usable_key_table(key: str | None, usable: bool) -> None:
    assert is_usable_openai_key(key) is usable


def test_boot_worker_staging_without_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert NO_KEY in _refusal(monkeypatch, _env("staging", "worker"))


def test_boot_worker_staging_placeholder_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env("production", "worker", OPENAI_API_KEY="sk-REPLACE_ME")
    assert NO_KEY in _refusal(monkeypatch, env)


def test_boot_worker_staging_with_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _boot(monkeypatch, **_env("staging", "worker", OPENAI_API_KEY=CANARY_KEY)) is not None


def test_boot_worker_local_without_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _boot(monkeypatch, **_env("local", "worker")) is not None


def test_boot_api_staging_without_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _boot(monkeypatch, **_env("staging", "api")) is not None


def test_boot_unknown_embedding_model_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env("local", "worker", EMBEDDING_MODEL="text-embedding-3-large")
    assert NO_PRICE in _refusal(monkeypatch, env)
    # the API never embeds a document, so it boots with any model name
    assert _boot(monkeypatch, **_env("local", "api", EMBEDDING_MODEL="x-model")) is not None


def test_boot_error_has_no_key_value(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("staging", "worker", OPENAI_API_KEY=f"{CANARY_KEY}REPLACE_ME")
    env["EMBEDDING_MODEL"] = "canary-model-x"
    message = _refusal(monkeypatch, env)
    assert NO_KEY in message and NO_PRICE in message
    assert CANARY_KEY not in message and "canary-model-x" not in message
