"""S15 T15 (DW5, AC7-8): `refresh_account`, the worker's token refresh, against the in-process fake
marketplace as `app_rw`: refreshed, rejected (needs_reconnect, one task, one alert event),
transient, the races (lock held, revoked mid-flight) and the `credential_used` audit row that is
written exactly once whenever the credential was unsealed."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from tests.conftest import LogCapture, SentryCapture

from .auth_support import audit_rows
from .conftest import LOCAL_HOSTS
from .marketplace_support import MktWorld, account_rows, connected_account, mapi, mkt

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
DSN = "https://public@o0.ingest.example.invalid/0"


def _due(mw: MktWorld, account_id: str) -> None:
    mw.conn.execute(
        "UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute' WHERE id = %s",
        [account_id],
    )


def _row(mw: MktWorld, account_id: str) -> dict[str, Any]:
    return next(r for r in account_rows(mw.conn) if str(r["id"]) == account_id)


def _used(mw: MktWorld) -> list[tuple[Any, ...]]:
    return audit_rows(mw.conn, "marketplace_account.credential_used")


def _tasks(mw: MktWorld, account_id: str) -> list[tuple[Any, ...]]:
    return mw.conn.execute(
        "SELECT status FROM human_tasks WHERE marketplace_account_id = %s", [account_id]
    ).fetchall()


async def _refresh(mw: MktWorld, account_id: str, slug: str = "acme") -> str:
    from app.workers.token_refresh import refresh_account

    return await refresh_account(mw.tenant(slug), uuid.UUID(account_id))


@pytest.fixture
async def due_account(mkt: MktWorld, mapi: httpx.AsyncClient) -> dict[str, Any]:
    account = await connected_account(mapi, mkt, seller="A1REFRESH")
    _due(mkt, account["id"])
    return account


async def test_token_refresh_ok(mkt: MktWorld, due_account: dict[str, Any]) -> None:
    before = _row(mkt, due_account["id"])
    assert await _refresh(mkt, due_account["id"]) == "refreshed"
    after = _row(mkt, due_account["id"])
    assert bytes(after["token_enc"]) != bytes(before["token_enc"])
    assert after["expires_at"] > before["expires_at"]
    assert after["last_refresh_at"] is not None and after["last_refresh_error_code"] is None
    assert after["status"] == "active"
    [used] = _used(mkt)
    assert used[1] == "system" and str(used[5]) == due_account["id"]
    assert used[9] == "token_refresh"


async def test_refresh_failure_reconnect(
    mkt: MktWorld,
    due_account: dict[str, Any],
    sentry_events: SentryCapture,
    log_lines: Callable[..., LogCapture],
) -> None:
    """DW5: `invalid_grant` -> needs_reconnect, AUTH_EXPIRED, one reconnect task, one ERROR log
    line and one Sentry event with the four fields, exactly one `credential_used`."""
    from app.core.config import Settings
    from app.core.observability import init_sentry

    capture = log_lines()
    init_sentry(Settings(_env_file=None, app_env="local", app_role="worker", sentry_dsn=DSN))
    mkt.control.next_refresh = "invalid_grant"
    assert await _refresh(mkt, due_account["id"]) == "rejected"
    row = _row(mkt, due_account["id"])
    assert row["status"] == "needs_reconnect" and row["needs_reconnect_since"] is not None
    assert row["last_refresh_error_code"] == "AUTH_EXPIRED"
    assert row["token_enc"] is not None  # kept: the reconnect replaces it
    assert _tasks(mkt, due_account["id"]) == [("open",)]
    assert len(_used(mkt)) == 1
    assert len(audit_rows(mkt.conn, "marketplace_account.needs_reconnect")) == 1
    errors = [line for line in capture.lines() if line["severity"] == "ERROR"]
    assert [e["message"] for e in errors] == ["marketplace.token_refresh_failed"]
    fields = {k: errors[0][k] for k in ("tenant_id", "account_id", "marketplace", "error_code")}
    assert fields == {
        "tenant_id": str(mkt.tenant("acme")),
        "account_id": due_account["id"],
        "marketplace": "amazon",
        "error_code": "AUTH_EXPIRED",
    }
    import sentry_sdk

    sentry_sdk.get_client().flush(timeout=2)
    [event] = sentry_events.events()
    assert "marketplace.token_refresh_failed" in str(event.get("logentry") or event.get("message"))
    assert "CANARYzq9" not in sentry_events.serialized()


async def test_refresh_second_failure_adds_no_task(
    mkt: MktWorld, due_account: dict[str, Any]
) -> None:
    mkt.control.next_refresh = "invalid_grant"
    assert await _refresh(mkt, due_account["id"]) == "rejected"
    mkt.control.next_refresh = "invalid_grant"
    assert await _refresh(mkt, due_account["id"]) == "skipped"
    assert len(_tasks(mkt, due_account["id"])) == 1
    assert len(_used(mkt)) == 1


async def test_token_refresh_5xx_stays_active(mkt: MktWorld, due_account: dict[str, Any]) -> None:
    mkt.control.next_refresh = "server_error"
    assert await _refresh(mkt, due_account["id"]) == "transient"
    row = _row(mkt, due_account["id"])
    assert row["status"] == "active" and row["last_refresh_error_code"] == "MARKETPLACE_UNAVAILABLE"
    assert _tasks(mkt, due_account["id"]) == []
    assert len(_used(mkt)) == 1
    assert audit_rows(mkt.conn, "marketplace_account.needs_reconnect") == []


class _Timeout(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")


async def test_token_refresh_timeout_stays_active(
    mkt: MktWorld, due_account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.adapters.oauth.http.make_transport", _Timeout)
    assert await _refresh(mkt, due_account["id"]) == "transient"
    row = _row(mkt, due_account["id"])
    assert row["status"] == "active" and row["last_refresh_error_code"] == "MARKETPLACE_UNAVAILABLE"


class _BadGzip(httpx.AsyncBaseTransport):
    """A 200 whose gzip body does not decode: httpx raises DecodingError while the body streams."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            content=b"not-gzip",
        )


async def test_token_refresh_bad_encoding_audited_once(
    mkt: MktWorld, due_account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.adapters.oauth.http.make_transport", _BadGzip)
    assert await _refresh(mkt, due_account["id"]) == "transient"
    row = _row(mkt, due_account["id"])
    assert (
        row["status"] == "active" and row["last_refresh_error_code"] == "MARKETPLACE_UNKNOWN_ERROR"
    )
    assert len(_used(mkt)) == 1
    assert _tasks(mkt, due_account["id"]) == []


async def test_token_refresh_invalid_client_no_reconnect(
    mkt: MktWorld, due_account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_CLIENT_SECRET", "rotated-wrong")
    get_settings.cache_clear()
    assert await _refresh(mkt, due_account["id"]) == "transient"
    row = _row(mkt, due_account["id"])
    assert (
        row["status"] == "active" and row["last_refresh_error_code"] == "MARKETPLACE_UNKNOWN_ERROR"
    )
    assert _tasks(mkt, due_account["id"]) == []


async def test_token_refresh_lock_held_skipped(mkt: MktWorld, due_account: dict[str, Any]) -> None:
    from app.repositories.marketplace_accounts import refresh_lock

    async with refresh_lock(uuid.UUID(due_account["id"])) as held:
        assert held
        assert await _refresh(mkt, due_account["id"]) == "skipped"
    assert _used(mkt) == []
    assert mkt.control.refreshes == 0


async def _revoke_on_token_call(
    mkt: MktWorld, monkeypatch: pytest.MonkeyPatch, account_id: str
) -> None:
    """Revoke the account (admin connection) at the moment the worker calls the token endpoint."""

    class _RevokeFirst(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.ASGITransport(app=mkt.fake)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            mkt.conn.execute(
                "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
                "kms_key_version = NULL, revoked_at = now() WHERE id = %s",
                [account_id],
            )
            return await self.inner.handle_async_request(request)

    monkeypatch.setattr("app.adapters.oauth.http.make_transport", _RevokeFirst)


async def test_token_refresh_revoked_mid_flight_stale(
    mkt: MktWorld, due_account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _revoke_on_token_call(mkt, monkeypatch, due_account["id"])
    assert await _refresh(mkt, due_account["id"]) == "stale"
    row = _row(mkt, due_account["id"])
    assert row["status"] == "revoked" and row["token_enc"] is None
    assert row["kms_key_version"] is None
    assert len(_used(mkt)) == 1


async def test_token_refresh_5xx_cas_miss_still_audited(
    mkt: MktWorld, due_account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mkt.control.next_refresh = "server_error"
    await _revoke_on_token_call(mkt, monkeypatch, due_account["id"])
    assert await _refresh(mkt, due_account["id"]) == "stale"
    assert _row(mkt, due_account["id"])["last_refresh_error_code"] is None
    assert len(_used(mkt)) == 1


async def test_token_refresh_not_due_skipped(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    account = await connected_account(mapi, mkt)  # the fake's expires_in is an hour
    assert await _refresh(mkt, account["id"]) == "skipped"
    assert _used(mkt) == []


class _TxProbe(httpx.AsyncBaseTransport):
    def __init__(self, mkt: MktWorld) -> None:
        self.inner = httpx.ASGITransport(app=mkt.fake)
        self.conn = mkt.conn
        self.seen: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        row = self.conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
            "AND datname = current_database() AND state LIKE 'idle in transaction%'"
        ).fetchone()
        self.seen.append(int(row[0]) if row else -1)
        return await self.inner.handle_async_request(request)


async def test_token_refresh_no_transaction_during_http(
    mkt: MktWorld, due_account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _TxProbe(mkt)
    monkeypatch.setattr("app.adapters.oauth.http.make_transport", lambda: probe)
    assert await _refresh(mkt, due_account["id"]) == "refreshed"
    assert probe.seen == [0]


async def test_token_refresh_other_tenant_id_skipped(
    mkt: MktWorld, due_account: dict[str, Any]
) -> None:
    assert await _refresh(mkt, due_account["id"], slug="bharat") == "skipped"
    assert _used(mkt) == []
    assert mkt.control.refreshes == 0


async def test_token_refresh_tampered_blob_unseal_failed(
    mkt: MktWorld, due_account: dict[str, Any], log_lines: Callable[..., LogCapture]
) -> None:
    capture = log_lines()
    mkt.conn.execute(
        "UPDATE marketplace_accounts SET token_enc = set_byte(token_enc, 80, "
        "get_byte(token_enc, 80) # 1) WHERE id = %s",
        [due_account["id"]],
    )
    assert await _refresh(mkt, due_account["id"]) == "unseal_failed"
    row = _row(mkt, due_account["id"])
    assert row["last_refresh_error_code"] == "MARKETPLACE_UNKNOWN_ERROR"
    assert row["status"] == "active"
    assert mkt.control.refreshes == 0 and _used(mkt) == []
    errors = [line["message"] for line in capture.lines() if line["severity"] == "ERROR"]
    assert errors == ["marketplace.credential_unseal_failed"]
