"""S23 tester attacks on the analytics endpoint (AC1-AC4): odd `ErrorReport.retryable` shapes, the
in-flight slot after refused and failed requests, tenant hints in headers and parameters, and
query-string junk. Rows are placed in June 2026, outside every range the golden world reads."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from . import analytics_support as s
from .analytics_support import build_analytics_world
from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .products_support import bearer_for

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

JUNE = {"from": "2026-06-01", "to": "2026-06-30"}
URL = f"{PREFIX}/analytics/summary"


@pytest.fixture
def an(products_world: Any) -> Any:
    tenants = {slug: products_world.tenant(slug) for slug in ("acme", "bharat")}
    build_analytics_world(products_world.conn, tenants)
    return products_world


def _june(day: int) -> datetime:
    return datetime(2026, 6, day, 12, tzinfo=UTC)


def _error_row(conn: Any, side: s.Side, report: Any, day: int) -> None:
    """One `error` submission answered on June `day` whose `error_report_json` is `report`."""
    listing = s.listing(conn, side)
    version = s.version(conn, side, listing, s.SETUP)
    row = s.submission(conn, side, listing, version, "error", _june(day))
    conn.execute(
        "UPDATE listing_submissions SET error_report_json = %s::jsonb WHERE id = %s",
        [json.dumps(report), row],
    )


async def test_analytics_attack_retryable_only_a_true_boolean_excludes(
    an: Any, api: httpx.AsyncClient
) -> None:
    """Only a top-level JSON `true` marks an `error` row retryable: a string, a number, a nested
    key, `false` or an array all count as a failed publish, and the request never 500s."""
    conn = an.conn
    conn.execute("SET session_replication_role = replica")
    try:
        side = s._side(conn, an.tenant("acme"))
        shapes: list[Any] = [
            {"retryable": True, "error_code": "RATE_LIMITED"},  # excluded
            {"retryable": "true"},
            {"retryable": 1},
            {"details": {"retryable": True}},
            {"retryable": False},
            {"retryable": None},
            [{"retryable": True}],
        ]
        for day, report in enumerate(shapes, start=1):
            _error_row(conn, side, report, day)
    finally:
        conn.execute("RESET session_replication_role")
    response = await api.get(URL, headers=bearer_for(an, "owner@acme.test"), params=JUNE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["publish_succeeded"] == 0
    assert body["counts"]["publish_failed"] == len(shapes) - 1
    assert body["publish_success_rate"] == 0.0


async def test_analytics_attack_tenant_hints_ignored(an: Any, api: httpx.AsyncClient) -> None:
    """The tenant comes only from the token: a bharat id in the query, in tenant headers or a
    repeated `from` never changes acme's answer."""
    acme_headers = bearer_for(an, "owner@acme.test")
    bharat = str(an.tenant("bharat"))
    base = await api.get(
        URL, headers=acme_headers, params={"from": "2026-09-01", "to": "2026-09-30"}
    )
    assert base.status_code == 200
    hinted = await api.get(
        URL,
        headers={**acme_headers, "X-Tenant-ID": bharat, "X-Tenant": bharat},
        params=[
            ("from", "2026-09-01"),
            ("to", "2026-09-30"),
            ("tenant_id", bharat),
            ("tid", bharat),
        ],
    )
    assert hinted.status_code == 200
    assert hinted.json() == base.json()


async def test_analytics_attack_inflight_slot_never_leaks(an: Any, api: httpx.AsyncClient) -> None:
    """Refused (422) and answered (200) requests leave the tenant's in-flight counter at 0, so a
    long run of them can never lock the tenant out with 429."""
    acme = an.tenant("acme")
    headers = bearer_for(an, "owner@acme.test")
    for params in (
        {"from": "2026-09-02", "to": "2026-09-01"},
        {"from": "2020-01-01", "to": "2026-01-01"},
        {"from": "x" * 4096},
        {"from": "2026-09-01\u0000", "to": "2026-09-30"},
        {"from": "٢٠٢٦-09-01"},  # Arabic-Indic digits
        JUNE,
        JUNE,
        JUNE,
    ):
        response = await api.get(URL, headers=headers, params=params)
        assert response.status_code in (200, 422), (params, response.text)
        assert "Traceback" not in response.text
    rows = an.conn.execute(
        "SELECT coalesce(max(n), 0) FROM rate_limit_counters WHERE bucket_key = %s",
        [f"an:{acme}"],
    ).fetchone()
    assert rows is not None and rows[0] == 0
    for _ in range(3):
        assert (await api.get(URL, headers=headers, params=JUNE)).status_code == 200


async def test_analytics_attack_oversized_param_is_refused(an: Any, api: httpx.AsyncClient) -> None:
    """A ~60 KB `to` is refused 422 with the one envelope, never a 500."""
    response = await api.get(
        URL, headers=bearer_for(an, "owner@acme.test"), params={"to": "9" * 60000}
    )
    assert response.status_code in (414, 422, 431), response.status_code
    if response.status_code == 422:
        assert response.json()["error"]["type"] == "VALIDATION_FAILED"


async def test_analytics_attack_single_day_and_leap_day(an: Any, api: httpx.AsyncClient) -> None:
    """`from == to` is one day; Feb 29 of a leap year is a valid bound, of a common year 422."""
    headers = bearer_for(an, "owner@acme.test")
    one = await api.get(URL, headers=headers, params={"from": "2026-09-30", "to": "2026-09-30"})
    assert one.status_code == 200
    assert one.json()["from"] == one.json()["to"] == date(2026, 9, 30).isoformat()
    leap = await api.get(URL, headers=headers, params={"from": "2028-02-29", "to": "2028-02-29"})
    assert leap.status_code == 200
    common = await api.get(URL, headers=headers, params={"from": "2027-02-29"})
    assert common.status_code == 422
