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
