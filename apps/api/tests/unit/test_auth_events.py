"""DPP-1 T10 (AC22): every security event writes its audit row and one structured log line whose
message is the action, with an IP hash and never the email, address or a secret."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any, ClassVar

import pytest
from app.services import auth_events
from structlog.testing import capture_logs

USER = uuid.uuid4()
TENANT = uuid.uuid4()


class FakeAudit:
    rows: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, _: Any) -> None:
        pass

    async def write(self, **kw: Any) -> None:
        FakeAudit.rows.append(kw)


@pytest.fixture(autouse=True)
def _audit(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAudit.rows = []
    monkeypatch.setattr(auth_events, "AuditRepository", FakeAudit)


def test_log_line_has_action_ip_hash_no_email() -> None:
    with capture_logs() as logs:
        asyncio.run(
            auth_events.record(
                object(), action="auth.locked", user_id=USER, tenant_id=None, ip="203.0.113.7"
            )
        )
    [line] = logs
    assert line["event"] == "auth.locked"
    assert line["user_id"] == str(USER) and line["tenant_id"] is None
    assert line["ip_hash"] == hashlib.sha256(b"203.0.113.7").hexdigest()[:16]
    text = repr(line)
    assert "203.0.113.7" not in text and "@" not in text
    [row] = FakeAudit.rows
    assert row["action"] == "auth.locked" and row["entity_type"] == "user"
    # no tenant: the platform-actor CHECK allows only a system or staff actor
    assert row["actor_type"] == "system" and row["actor_user_id"] is None
    assert row["ip"] == "203.0.113.7"


def test_user_actor_inside_a_tenant() -> None:
    asyncio.run(
        auth_events.record(
            object(), action="auth.step_up", user_id=USER, tenant_id=TENANT, ip=None, reason="otp"
        )
    )
    [row] = FakeAudit.rows
    assert (row["actor_type"], row["actor_user_id"], row["reason"]) == ("user", USER, "otp")


def test_log_event_without_audit_and_warning_level() -> None:
    with capture_logs() as logs:
        auth_events.log_event(
            "auth.attempt_busy", user_id=USER, tenant_id=None, ip=None, warning=True
        )
    [line] = logs
    assert line["log_level"] == "warning" and line["ip_hash"] is None
    assert FakeAudit.rows == []


def test_ip_hash_is_stable_and_short() -> None:
    assert auth_events.ip_hash("10.0.0.1") == auth_events.ip_hash("10.0.0.1")
    assert len(auth_events.ip_hash("10.0.0.1") or "") == 16
    assert auth_events.ip_hash(None) is None
