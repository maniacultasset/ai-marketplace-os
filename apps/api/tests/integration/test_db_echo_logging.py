"""S11 T5: with `DATABASE_ECHO=true`, SQL lines go through the JSON chain only (never SQLAlchemy's
own plain-text stdout handler), carry the request id, and never show bound parameters."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from tests.conftest import LogCapture

if TYPE_CHECKING:
    from app.core.config import Settings

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


async def test_database_echo_lines_are_json(
    db_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Callable[..., LogCapture],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.core.config import get_settings
    from app.core.request_context import request_context
    from app.db.session import dispose_engine, get_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_ECHO", "true")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.database_echo is True
    capture = log_lines(database_echo=True)
    capsys.readouterr()

    with request_context("abc"):
        async with get_engine().connect() as connection:
            await connection.execute(
                text("SELECT CAST(:email AS text) AS e, CAST(:pw AS text) AS p"),
                {"email": "a@b.example", "pw": "$argon2id$Zq9"},
            )

    for name in ("sqlalchemy.engine", "sqlalchemy.engine.Engine"):
        handlers = logging.getLogger(name).handlers
        assert all(type(h) is logging.NullHandler for h in handlers), (name, handlers)
    lines = capture.lines()
    sql_lines = [line for line in lines if str(line["module"]).startswith("sqlalchemy.engine")]
    assert any("SELECT CAST" in line["message"] for line in sql_lines)
    assert all(line["request_id"] == "abc" for line in sql_lines)
    assert "a@b" not in capture.text()
    assert "Zq9" not in capture.text()
    assert capsys.readouterr().out == ""
    await dispose_engine()
