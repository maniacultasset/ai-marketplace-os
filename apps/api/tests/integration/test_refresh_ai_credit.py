"""SI10 T12 (AC4): the periodic OpenRouter `/key` check, against a fake transport (no network, no
real key): it records the numbers, clears a pause only by the CP-D3 rule, pauses on a refused key
and alerts at 50/80/95 % used and below the floor, each once per window."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
NOW = datetime(2026, 11, 7, 12, 7, tzinfo=UTC)


class KeyServer(httpx.AsyncBaseTransport):
    """The `/key` answer: a status and the credit numbers."""

    def __init__(self) -> None:
        self.status = 200
        self.limit: float | None = 10
        self.remaining: float | None = 6
        self.usage = 4.0
        self.timeout = False
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.timeout:
            raise httpx.ReadTimeout("slow", request=request)
        body = {
            "data": {"limit": self.limit, "limit_remaining": self.remaining, "usage": self.usage}
        }
        return httpx.Response(self.status, json=body, request=request)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[KeyServer]:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_GATEWAY", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "Zq9openrouter-test")
    get_settings.cache_clear()
    yield KeyServer()
    get_settings.cache_clear()


async def _refresh(server: KeyServer, now: datetime = NOW) -> str:
    from app.workers.periodic import refresh_ai_credit

    return await refresh_ai_credit(now, transport=server)


def _row(conn: Any) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM ai_credit_state WHERE id = 1")
    names = [d.name for d in cur.description or ()]
    return dict(zip(names, cur.fetchone(), strict=True))


def _seed(conn: Any, *, reason: str | None, remaining: str | None) -> None:
    conn.execute(
        "UPDATE ai_credit_state SET paused_reason = %s, "
        "paused_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, "
        "remaining_usd = %s::numeric WHERE id = 1",
        [reason, reason, remaining],
    )


def _alerts(capture: Any, kind: str) -> list[dict[str, Any]]:
    return [line for line in capture.lines() if line["message"] == f"alert.{kind}"]


async def test_200_records_numbers_and_checked_at(jobs_db: JobWorld, server: KeyServer) -> None:
    assert await _refresh(server) == "recorded"
    row = _row(jobs_db.conn)
    assert (row["limit_usd"], row["usage_usd"], row["remaining_usd"]) == (
        Decimal("10"),
        Decimal("4"),
        Decimal("6"),
    )
    assert row["checked_at"] == NOW
    assert row["paused_reason"] is None


async def test_credit_pause_clears_on_a_rise_against_the_previous_value(
    jobs_db: JobWorld, server: KeyServer
) -> None:
    _seed(jobs_db.conn, reason="credit_exhausted", remaining="0.40")
    server.remaining = 6.0
    assert await _refresh(server) == "cleared"
    row = _row(jobs_db.conn)
    assert (row["paused_reason"], row["remaining_usd"]) == (None, Decimal("6"))
    # paused again (say a 402) with the same remaining: a flat value never clears
    jobs_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now()"
    )
    assert await _refresh(server, NOW + timedelta(minutes=5)) == "recorded"
    assert _row(jobs_db.conn)["paused_reason"] == "credit_exhausted"


async def test_flat_5_stays_paused(jobs_db: JobWorld, server: KeyServer) -> None:
    _seed(jobs_db.conn, reason="credit_exhausted", remaining="5.00")
    server.remaining = 5.0
    await _refresh(server)
    assert _row(jobs_db.conn)["paused_reason"] == "credit_exhausted"


async def test_unlimited_key_never_clears_credit_pause(
    jobs_db: JobWorld, server: KeyServer
) -> None:
    _seed(jobs_db.conn, reason="credit_exhausted", remaining="0.40")
    server.limit, server.remaining = None, None
    await _refresh(server)
    row = _row(jobs_db.conn)
    assert row["paused_reason"] == "credit_exhausted"
    assert row["remaining_usd"] is None  # recorded as unlimited


async def test_key_invalid_clears_on_any_200(jobs_db: JobWorld, server: KeyServer) -> None:
    _seed(jobs_db.conn, reason="key_invalid", remaining=None)
    server.limit, server.remaining = None, None
    assert await _refresh(server) == "cleared"
    assert _row(jobs_db.conn)["paused_reason"] is None


async def test_401_pauses_key_invalid_one_alert_per_hour(
    jobs_db: JobWorld, server: KeyServer, log_lines: Any
) -> None:
    capture = log_lines()
    server.status = 401
    assert await _refresh(server) == "refused"
    assert _row(jobs_db.conn)["paused_reason"] == "key_invalid"
    assert await _refresh(server, NOW + timedelta(minutes=5)) == "refused"
    lines = _alerts(capture, "llm_provider_refused")
    assert len(lines) == 1 and lines[0]["severity"] == "ERROR"
    assert lines[0]["error_code"] == "AI_PROVIDER_KEY_INVALID"


async def test_usage_thresholds_once_per_day(
    jobs_db: JobWorld, server: KeyServer, log_lines: Any
) -> None:
    capture = log_lines()
    for remaining, minute in ((5.0, 0), (2.0, 5), (0.5, 10), (0.5, 15)):
        server.remaining, server.usage = remaining, 10 - remaining
        await _refresh(server, NOW + timedelta(minutes=minute))
    pcts = sorted(int(line["threshold_pct"]) for line in _alerts(capture, "llm_credit"))
    assert pcts == [50, 80, 95]
    assert all(line["severity"] == "WARNING" for line in _alerts(capture, "llm_credit"))
    assert _alerts(capture, "llm_provider_refused") == []  # 0.50 is the floor, not below


async def test_below_floor_alerts_and_admission_refuses(
    jobs_db: JobWorld, server: KeyServer, log_lines: Any
) -> None:
    from app.workers.budget import admission

    capture = log_lines()
    server.remaining, server.usage = 0.4, 9.6
    await _refresh(server, datetime.now(UTC))
    low = _alerts(capture, "llm_provider_refused")
    assert len(low) == 1 and low[0]["remaining_usd"] == "0.4"
    assert await admission(jobs_db.tenant("acme"), datetime.now(UTC)) == "credit_exhausted"


@pytest.mark.parametrize("failure", ["500", "timeout"])
async def test_unavailable_leaves_row_untouched(
    jobs_db: JobWorld, server: KeyServer, log_lines: Any, failure: str
) -> None:
    capture = log_lines()
    _seed(jobs_db.conn, reason="credit_exhausted", remaining="0.40")
    before = _row(jobs_db.conn)
    if failure == "500":
        server.status = 500
    else:
        server.timeout = True
    assert await _refresh(server) == "unavailable"
    assert _row(jobs_db.conn) == before
    assert [line for line in capture.lines() if line["message"].startswith("alert.")] == []
    assert any(line["message"] == "ai_credit.check_failed" for line in capture.lines())


async def test_direct_gateway_or_no_key_makes_no_call(
    jobs_db: JobWorld, server: KeyServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_GATEWAY", "direct")
    get_settings.cache_clear()
    assert await _refresh(server) == "skipped"
    monkeypatch.setenv("LLM_GATEWAY", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    get_settings.cache_clear()
    assert await _refresh(server) == "skipped"
    assert server.calls == 0
