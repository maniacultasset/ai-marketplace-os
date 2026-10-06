"""S11 T5: the JSON log chain (structlog and the stdlib), its fields and its redaction."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
import structlog
from tests.conftest import LogCapture

Start = Callable[..., LogCapture]
ISO_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")
SECRETS = {
    "AMAZON_CLIENT_SECRET": "Zq9amazon",
    "access_token": "Zq9access",
    "refresh_token": "Zq9refresh",
    "hashed_password": "Zq9hash",
    "token_enc": "Zq9enc",
    "Authorization": "Bearer Zq9auth",
}


def _log() -> Any:
    from app.core.logging import get_logger

    return get_logger("app.tests.logging")


def test_log_json_shape(log_lines: Start) -> None:
    from app.core.request_context import request_context

    capture = log_lines()
    with request_context("abc"):
        _log().info("hello", extra_field=7)
    [line] = capture.lines()
    assert ISO_UTC.fullmatch(line["timestamp"])
    assert line["severity"] == "INFO"
    assert line["message"] == "hello"
    assert line["module"] == "app.tests.logging"
    assert line["request_id"] == "abc"
    assert line["extra_field"] == 7
    assert "level" not in line
    assert "event" not in line
    assert list(line)[:4] == ["timestamp", "severity", "message", "module"]


def test_log_outside_request_has_no_request_id(log_lines: Start) -> None:
    capture = log_lines()
    _log().info("outside")
    [line] = capture.lines()
    assert "request_id" not in line
    assert "tenant_id" not in line


def test_tenant_id_only_when_bound(log_lines: Start) -> None:
    from app.core.request_context import bind_tenant, request_context

    capture = log_lines()
    with request_context("abc"):
        _log().info("before")
        bind_tenant("8a6e0804-2bd0-4672-b79d-d97027f9071a")
        _log().info("after")
    before, after = capture.lines()
    assert "tenant_id" not in before
    assert after["tenant_id"] == "8a6e0804-2bd0-4672-b79d-d97027f9071a"


@pytest.mark.parametrize("name", ["app.x", "sqlalchemy.engine", "uvicorn.error"])
def test_stdlib_lines_carry_request_id(log_lines: Start, name: str) -> None:
    from app.core.request_context import request_context

    capture = log_lines()
    with request_context("abc"):
        logging.getLogger(name).warning("stdlib %s", "line")
    [line] = capture.lines()
    assert line["request_id"] == "abc"
    assert line["module"] == name
    assert line["message"] == "stdlib line"
    assert line["severity"] == "WARNING"


def test_redaction_logs_structlog_kwargs(log_lines: Start) -> None:
    capture = log_lines()
    _log().info("kwargs", **SECRETS, nested={"headers": [["authorization", "Bearer Zq9x"]]})
    text = capture.text()
    assert "Zq9" not in text
    [line] = capture.lines()
    for key in SECRETS:
        assert line[key] == "[REDACTED]"


def test_redaction_logs_stdlib_extra(log_lines: Start) -> None:
    capture = log_lines()
    logging.getLogger("app.extra").warning("extra", extra=dict(SECRETS))
    assert "Zq9" not in capture.text()
    [line] = capture.lines()
    assert line["access_token"] == "[REDACTED]"


def test_redaction_logs_message_bearer(log_lines: Start) -> None:
    capture = log_lines()
    _log().info("calling with Bearer Zq9tok and postgresql://u:Zq9pw@h/db")
    logging.getLogger("app.x").info("header %s", "Bearer Zq9std")
    assert "Zq9" not in capture.text()


def test_redaction_logs_exception_text(log_lines: Start) -> None:
    capture = log_lines()
    try:
        raise ValueError("Bearer Zq9exc")
    except ValueError:
        _log().exception("structlog failure")
        logging.getLogger("app.x").exception("stdlib failure")
    lines = capture.lines()
    assert len(lines) == 2
    for line in lines:
        assert "ValueError" in line["exception"]
        assert "Zq9" not in line["exception"]
        assert line["severity"] == "ERROR"


def test_redaction_logs_object_repr(log_lines: Start) -> None:
    class Holder:
        def __repr__(self) -> str:
            return "Holder(token=Bearer Zq9obj)"

    capture = log_lines()
    _log().info("object", holder=Holder())
    assert "Zq9" not in capture.text()


def test_redaction_logs_keeps_tokens_in(log_lines: Start) -> None:
    capture = log_lines()
    _log().info("usage", tokens_in=120, tokens_out=30, request_id_hint="r")
    [line] = capture.lines()
    assert line["tokens_in"] == 120
    assert line["tokens_out"] == 30


def test_settings_object_logged_shows_no_secret(log_lines: Start) -> None:
    from app.core.config import Settings

    capture = log_lines()
    settings = Settings(
        _env_file=None,
        app_env="local",
        app_role="shell",
        jwt_secret="Zq9jwt_" + "j" * 32,
        database_url="postgresql+psycopg://u:Zq9db@h:5432/x",
        sentry_dsn="https://Zq9sentry@o0.ingest.example.invalid/0",
    )
    _log().info("settings", settings=settings)
    assert "Zq9" not in capture.text()


def test_configure_logging_idempotent(log_lines: Start) -> None:
    log_lines()
    capture = log_lines()
    own = [h for h in logging.getLogger().handlers if getattr(h, "_mkos_root_handler", False)]
    assert len(own) == 1
    _log().info("once")
    assert len(capture.lines()) == 1


def test_log_level_applied(log_lines: Start) -> None:
    capture = log_lines(log_level="WARNING")
    _log().info("dropped")
    logging.getLogger("app.x").info("dropped too")
    _log().warning("kept")
    assert [line["message"] for line in capture.lines()] == ["kept"]


def test_log_level_lowercase_applied(log_lines: Start) -> None:
    capture = log_lines(log_level="error")
    _log().warning("dropped")
    _log().error("kept")
    assert [line["message"] for line in capture.lines()] == ["kept"]


def test_console_format_renders(log_lines: Start) -> None:
    from app.core.request_context import request_context

    capture = log_lines(log_format="console")
    with request_context("abc"):
        _log().info("console line", access_token="Zq9c")
    text = capture.text()
    assert "abc" in text
    assert "console line" in text
    assert "Zq9" not in text
    assert not text.lstrip().startswith("{")


def test_uvicorn_access_logger_silenced(log_lines: Start) -> None:
    capture = log_lines()
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /x?token=Zq9 HTTP/1.1" 200')
    assert capture.text() == ""
    for name in ("uvicorn", "uvicorn.error"):
        lib = logging.getLogger(name)
        assert lib.handlers == []
        assert lib.propagate is True


def test_echo_parameters_never_logged(log_lines: Start) -> None:
    """With `DATABASE_ECHO`, SQLAlchemy logs bound parameters as `[%s] %r`; a password hash or an
    email there has no key the redactor could see, so the line keeps only its cache stats."""
    capture = log_lines(database_echo=True)
    engine_log = logging.getLogger("sqlalchemy.engine.Engine")
    engine_log.info("INSERT INTO users (email, hashed_password) VALUES (%s, %s)")
    engine_log.info("[%s] %r", "generated in 0.001s", ("a@b.example", "$argon2id$Zq9"))
    text = capture.text()
    assert "a@b" not in text
    assert "Zq9" not in text
    stats = capture.lines()[1]["message"]
    assert stats == "[generated in 0.001s] [SQL parameters hidden]"


def test_database_echo_off_level_warning(log_lines: Start) -> None:
    log_lines(database_echo=False)
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
    log_lines(database_echo=True)
    assert logging.getLogger("sqlalchemy.engine").level == logging.INFO


def test_sqlalchemy_engine_logger_has_no_writing_handler(log_lines: Start) -> None:
    log_lines(database_echo=True)
    for name in ("sqlalchemy.engine", "sqlalchemy.engine.Engine"):
        handlers = logging.getLogger(name).handlers
        assert all(type(h) is logging.NullHandler for h in handlers), name
    # SQLAlchemy adds its stdout handler only to a logger without handlers.
    assert logging.getLogger("sqlalchemy.engine.Engine").handlers


def test_structlog_configured_once_per_call(log_lines: Start) -> None:
    log_lines()
    assert structlog.is_configured()
