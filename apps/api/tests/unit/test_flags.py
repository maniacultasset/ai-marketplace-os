"""S22 T1 (AC13; D1, D2): the Flipkart flag and the one helper that reads it."""

from __future__ import annotations

from pathlib import Path

import pytest

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
    from app.core.config import get_settings

    base = {"APP_ENV": "local", "APP_ROLE": "api", "DATABASE_URL": LOCAL_URL}
    base["JWT_SECRET"] = PRIVATE_JWT
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


@pytest.mark.parametrize("app_env", ["local", "staging", "production"])
def test_flipkart_enabled_default_false(app_env: str) -> None:
    from app.core.config import Settings

    assert Settings.model_fields["flipkart_enabled"].default is False
    loaded = Settings.model_validate({"app_env": app_env}, context=None)
    assert loaded.flipkart_enabled is False


@pytest.mark.parametrize(
    ("raw", "expected"), [("true", True), ("false", False), ("1", True), ("0", False)]
)
def test_flipkart_enabled_parses(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    loaded = _settings(monkeypatch, FLIPKART_ENABLED=raw)
    assert loaded.flipkart_enabled is expected  # type: ignore[attr-defined]


def test_flipkart_enabled_rejects_garbage(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as info:
        _settings(monkeypatch, FLIPKART_ENABLED="yes please")
    assert isinstance(info.value.code, str)
    assert "FLIPKART_ENABLED" in info.value.code and "yes please" not in info.value.code


def test_marketplace_enabled_amazon_always() -> None:
    from app.core.config import Settings
    from app.core.flags import marketplace_enabled
    from mkos_schemas.enums import Marketplace

    for flag in (True, False):
        settings = Settings.model_validate({"app_env": "local", "flipkart_enabled": flag})
        assert marketplace_enabled(Marketplace.AMAZON, settings) is True
        assert marketplace_enabled("amazon", settings) is True


def test_marketplace_enabled_flipkart_follows_flag() -> None:
    from app.core.config import Settings
    from app.core.flags import enabled_marketplaces, marketplace_enabled

    on = Settings.model_validate({"app_env": "local", "flipkart_enabled": True})
    off = Settings.model_validate({"app_env": "local", "flipkart_enabled": False})
    assert marketplace_enabled("flipkart", on) is True
    assert marketplace_enabled("flipkart", off) is False
    assert enabled_marketplaces(on) == ("amazon", "flipkart")
    assert enabled_marketplaces(off) == ("amazon",)


def test_marketplace_enabled_unknown_marketplace_refused() -> None:
    from app.core.config import Settings
    from app.core.flags import marketplace_enabled

    settings = Settings.model_validate({"app_env": "local", "flipkart_enabled": True})
    with pytest.raises(ValueError):
        marketplace_enabled("ebay", settings)


def test_marketplace_not_enabled_error_shape() -> None:
    from app.api.errors import ApiError, marketplace_not_enabled
    from app.core.flags import NOT_ENABLED_MESSAGE, not_enabled_report
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorSource

    error = marketplace_not_enabled("flipkart")
    assert isinstance(error, ApiError)
    assert error.status == 501 and error.code is ErrorCode.NOT_IMPLEMENTED
    assert error.message == NOT_ENABLED_MESSAGE == "Flipkart is not enabled"
    report = not_enabled_report("flipkart")
    (item,) = report.errors
    assert report.status == "failed" and report.error_code is ErrorCode.NOT_IMPLEMENTED
    assert item.source is ErrorSource.SYSTEM and item.field == "listing"
    for text in (error.message, item.message):
        assert "http" not in text and "/" not in text
