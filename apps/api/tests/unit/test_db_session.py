"""AC6b: `ping` is the readiness query; it never leaks driver text and never eats cancellation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest


async def test_ping_true_on_select_1(
    ci_env: dict[str, str], fake_engine: Callable[..., Any]
) -> None:
    from app.db.session import ping

    engine = fake_engine()
    assert await ping(1.0) is True
    assert engine.executed == ["SELECT 1"]


async def test_ping_false_logs_class_name_only(
    ci_env: dict[str, str], fake_engine: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    from app.db.session import ping

    class DriverError(Exception):
        pass

    fake_engine(error=DriverError("FATAL: host=db user=postgres password=Zq9 failed"))
    with caplog.at_level(logging.DEBUG):
        assert await ping(1.0) is False
    assert "DriverError" in caplog.text
    assert "Zq9" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_ping_lets_cancellation_propagate(
    ci_env: dict[str, str], fake_engine: Callable[..., Any]
) -> None:
    from app.db.session import ping

    engine = fake_engine(delay=10.0)
    task = asyncio.create_task(ping(5.0))
    await engine.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_engine_settings_match_env_register(
    ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENV.md:36-41: pool 5/5, 10 s pool timeout, 1800 s recycle, pre-ping, and the statement
    timeout as a libpq `options` connect argument."""
    from app.db import session

    captured: dict[str, Any] = {}

    def fake_create(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session, "create_async_engine", fake_create)
    monkeypatch.setattr(session, "instrument_engine", lambda engine: None)  # S23 DB spans
    session.get_engine.cache_clear()
    try:
        session.get_engine()
    finally:
        session.get_engine.cache_clear()
    assert captured["url"] == ci_env["DATABASE_URL"]
    assert captured["pool_size"] == 5
    assert captured["max_overflow"] == 5
    assert captured["pool_timeout"] == 10
    assert captured["pool_recycle"] == 1800
    assert captured["pool_pre_ping"] is True
    assert captured["connect_args"] == {"options": "-c statement_timeout=15000"}


def test_engine_hides_parameters_and_never_echoes(
    ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11 C5: bound parameters (a hashed_password INSERT) never reach a log line, and `echo=` is
    not passed even with DATABASE_ECHO=true: configure_logging owns the sqlalchemy.engine level,
    so SQLAlchemy never attaches its own plain-text stdout handler."""
    from app.db import session

    monkeypatch.setenv("DATABASE_ECHO", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    def fake_create(url: str, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session, "create_async_engine", fake_create)
    monkeypatch.setattr(session, "instrument_engine", lambda engine: None)  # S23 DB spans
    session.get_engine.cache_clear()
    try:
        assert get_settings().database_echo is True
        session.get_engine()
    finally:
        session.get_engine.cache_clear()
        get_settings.cache_clear()
    assert captured["hide_parameters"] is True
    assert "echo" not in captured
