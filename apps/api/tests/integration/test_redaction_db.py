"""S11 T9 (AC 9): a real unique violation on `users (lower(email))`, or a NOT NULL violation that
lists the whole `users` row, inside a request leaves no part of the email, the password hash (or
any bound parameter) in the log line or in the Sentry event."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import LogCapture, SentryCapture

from .conftest import ScratchDb

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

DSN = "https://public@o0.ingest.example.invalid/0"
# Module constants, so the traceback's quoted source lines name them instead of their values.
EMAILS = ("Zq9.Person@Example.test", "zq9.person@example.TEST")
HASH = "$argon2id$Zq9hash"


@pytest.fixture
async def head_app(
    head_db: ScratchDb,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: SentryCapture,
) -> AsyncIterator[FastAPI]:
    """`create_app()` on the head scratch database, Sentry on, plus a route that writes two users
    whose emails differ only in case."""
    from app.core.config import get_settings
    from app.db.models.tenancy import User
    from app.db.session import dispose_engine, get_engine
    from app.main import create_app
    from sqlalchemy import insert

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", head_db.url)
    monkeypatch.setenv("JWT_SECRET", "ci_only_not_a_real_secret_at_least_32_chars")
    monkeypatch.setenv("SENTRY_DSN", DSN)
    get_settings.cache_clear()
    await dispose_engine()
    app = create_app()

    async def duplicate() -> None:
        async with get_engine().begin() as conn:
            for email in EMAILS:
                row = {"email": email, "hashed_password": HASH}
                await conn.execute(insert(User).values(row))

    async def null_flag() -> None:
        async with get_engine().begin() as conn:
            row = {"email": EMAILS[0], "hashed_password": HASH, "is_active": None}
            await conn.execute(insert(User).values(row))

    app.add_api_route("/__test/duplicate-user", duplicate, methods=["POST"])
    app.add_api_route("/__test/null-flag-user", null_flag, methods=["POST"])
    yield app
    await dispose_engine()
    get_settings.cache_clear()


async def test_unique_violation_detail_redacted(
    head_app: FastAPI, sentry_events: SentryCapture, log_lines: Callable[..., LogCapture]
) -> None:
    capture = log_lines()
    transport = httpx.ASGITransport(app=head_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/__test/duplicate-user", headers={"X-Request-ID": "abc"})
    assert response.status_code == 500

    [error] = [line for line in capture.lines() if line["severity"] == "ERROR"]
    assert error["request_id"] == "abc"
    traceback = error["exception"]
    assert "UniqueViolation" in traceback or "IntegrityError" in traceback
    assert "uq_users_email_lower" in traceback
    assert "Key (lower(email))=([REDACTED])" in traceback
    # C5: the engine hides bound values, so SQLAlchemy never renders them at all
    assert "[SQL parameters hidden due to hide_parameters=True]" in traceback
    assert "[parameters:" not in traceback

    [event] = sentry_events.events()
    assert event["tags"]["request_id"] == "abc"
    sent = sentry_events.serialized()
    assert "uq_users_email_lower" in sent
    for text in (capture.text(), sent):
        lowered = text.lower()
        assert "zq9" not in lowered
        assert "person@" not in lowered
        assert "argon2id" not in lowered


async def test_failing_row_detail_redacted(
    head_app: FastAPI, sentry_events: SentryCapture, log_lines: Callable[..., LogCapture]
) -> None:
    """A NOT NULL violation's `DETAIL:  Failing row contains (...)` lists every column."""
    capture = log_lines()
    transport = httpx.ASGITransport(app=head_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/__test/null-flag-user", headers={"X-Request-ID": "abd"})
    assert response.status_code == 500

    [error] = [line for line in capture.lines() if line["severity"] == "ERROR"]
    assert error["request_id"] == "abd"
    traceback = error["exception"]
    assert "NotNullViolation" in traceback or "IntegrityError" in traceback
    assert "is_active" in traceback
    assert "Failing row contains ([REDACTED])" in traceback

    [event] = sentry_events.events()
    assert event["tags"]["request_id"] == "abd"
    sent = sentry_events.serialized()
    assert "is_active" in sent
    for text in (capture.text(), sent):
        lowered = text.lower()
        assert "zq9" not in lowered
        assert "person@" not in lowered
        assert "argon2id" not in lowered
