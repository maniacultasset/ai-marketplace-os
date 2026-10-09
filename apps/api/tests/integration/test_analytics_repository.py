"""S23 T4-T5: `AnalyticsRepository` over the analytics world, as `app_rw` (RLS on)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from .analytics_support import (
    AFTER,
    BEFORE,
    EXPECTED,
    FIRST,
    FROM,
    LAST,
    TO,
    AnalyticsWorld,
    build_analytics_world,
    totals,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def world(auth_db: Conn) -> AnalyticsWorld:
    return build_analytics_world(auth_db)


def _bounds() -> tuple[datetime, datetime]:
    from app.services.analytics import DateRange

    return DateRange(FROM, TO).bounds()


async def _read[T](tenant: UUID, fn: Callable[[Any], Awaitable[T]]) -> T:
    from app.repositories.analytics import AnalyticsRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(tenant) as session:
        return await fn(AnalyticsRepository(session, tenant))


def _expected(slug: str, *keys: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for marketplace, per in EXPECTED[slug].items():
        values = tuple(per[k] for k in keys)
        if any(values):
            out[marketplace] = values[0] if len(values) == 1 else values
    return out


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_metrics_volume_golden(world: AnalyticsWorld, slug: str) -> None:
    lo, hi = _bounds()
    got = await _read(world.tenants[slug], lambda r: r.volume(lo, hi))
    assert got == _expected(slug, "listings_generated")


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_metrics_decisions_golden(world: AnalyticsWorld, slug: str) -> None:
    lo, hi = _bounds()
    got = await _read(world.tenants[slug], lambda r: r.decisions(lo, hi))
    assert got == _expected(slug, "approvals", "rejections")


async def test_analytics_boundaries_volume(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    # only the edge rows: [FIRST, FIRST+1µs) and [LAST, AFTER)
    tick = timedelta(microseconds=1)
    assert await _read(acme, lambda r: r.volume(FIRST, FIRST + tick)) == {"amazon": 1}
    assert await _read(acme, lambda r: r.volume(LAST, AFTER)) == {"amazon": 1}
    assert await _read(acme, lambda r: r.volume(BEFORE, FIRST)) == {"amazon": 1}
    assert await _read(acme, lambda r: r.volume(AFTER, AFTER + tick)) == {"amazon": 1}


async def test_analytics_excludes_edit_and_import(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    day10, day12 = datetime.fromisoformat("2026-09-10T00:00:00+00:00"), datetime.fromisoformat(
        "2026-09-12T00:00:00+00:00"
    )
    # the edit (10th) and import (11th) versions are the only versions in [10th, 12th)
    assert await _read(acme, lambda r: r.volume(day10, day12)) == {}


async def test_analytics_excludes_regenerate_requested(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    day4, day5 = datetime.fromisoformat("2026-09-04T00:00:00+00:00"), datetime.fromisoformat(
        "2026-09-05T00:00:00+00:00"
    )
    assert await _read(acme, lambda r: r.decisions(day4, day5)) == {}


async def test_analytics_other_tenant_rows_absent(world: AnalyticsWorld) -> None:
    """A second world's rows (two more tenants) never reach acme's reads."""
    lo, hi = _bounds()
    acme = world.tenants["acme"]
    before = await _read(acme, lambda r: r.volume(lo, hi))
    other = build_analytics_world(world.conn)
    assert await _read(acme, lambda r: r.volume(lo, hi)) == before
    assert await _read(other.tenants["bharat"], lambda r: r.volume(lo, hi)) == {"amazon": 3}


# ---- T5: validation, publish, the summary ----


async def _summary(tenant: UUID, start: date = FROM, end: date = TO) -> dict[str, Any]:
    from app.services.analytics import DateRange, summarize

    return await _read(tenant, lambda r: summarize(r, DateRange(start, end)))


def _golden(slug: str) -> dict[str, Any]:
    from app.services.analytics import Counts, _block

    per = {m: Counts(**c) for m, c in EXPECTED[slug].items()}
    total = sum(per.values(), Counts())
    return {
        "from": FROM,
        "to": TO,
        **_block(total),
        "counts": totals(slug),
        "by_marketplace": [
            {"marketplace": m, **_block(c), "counts": EXPECTED[slug][m]} for m, c in per.items()
        ],
    }


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_validation_golden(world: AnalyticsWorld, slug: str) -> None:
    lo, hi = _bounds()
    got = await _read(world.tenants[slug], lambda r: r.validation(lo, hi))
    assert got == _expected(slug, "validation_first_pass_passed", "validation_first_pass_total")


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_publish_golden(world: AnalyticsWorld, slug: str) -> None:
    lo, hi = _bounds()
    got = await _read(world.tenants[slug], lambda r: r.publish(lo, hi))
    assert got == _expected(slug, "publish_succeeded", "publish_failed")


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_analytics_metrics(world: AnalyticsWorld, slug: str) -> None:
    assert await _summary(world.tenants[slug]) == _golden(slug)


async def test_analytics_metrics_acme_hand_values(world: AnalyticsWorld) -> None:
    """The rates written out by hand (EXPECTED's arithmetic, not the code's)."""
    body = await _summary(world.tenants["acme"])
    assert body["listings_generated"] == 5
    assert (body["approval_rate"], body["validation_pass_rate"]) == (0.5, 0.5)
    assert body["publish_success_rate"] == 0.7143  # 5 / 7
    amazon, flipkart = body["by_marketplace"]
    assert amazon["marketplace"] == "amazon" and flipkart["marketplace"] == "flipkart"
    assert (amazon["approval_rate"], amazon["validation_pass_rate"]) == (0.6667, 0.6667)
    assert amazon["publish_success_rate"] == 0.6667  # 4 / 6
    assert (flipkart["approval_rate"], flipkart["validation_pass_rate"]) == (0.0, 0.0)
    assert flipkart["publish_success_rate"] == 1.0


async def test_analytics_boundaries(world: AnalyticsWorld) -> None:
    """Each source at both edges: one-day ranges on FROM and TO see the edge rows only."""
    acme = world.tenants["acme"]
    first = (await _summary(acme, FROM, FROM))["counts"]
    last = (await _summary(acme, TO, TO))["counts"]
    assert first["listings_generated"] == 1  # FIRST in, BEFORE out
    assert first["validation_first_pass_total"] == 1
    assert first["publish_succeeded"] == 1  # the flipkart accept at FIRST
    assert last["listings_generated"] == 1  # LAST in, AFTER out
    assert last["approvals"] == 1
    assert last["validation_first_pass_total"] == 1
    assert last["publish_succeeded"] == 1  # live at LAST; the reject at AFTER is out


async def test_analytics_null_rates(world: AnalyticsWorld) -> None:
    """A range with volume but no decisions: approval rate null, counts 0."""
    acme = world.tenants["acme"]
    day = date(2026, 9, 15)
    body = await _summary(acme, day, day)
    assert body["listings_generated"] == 1
    assert body["counts"]["approvals"] == body["counts"]["rejections"] == 0
    assert body["approval_rate"] is None


async def test_analytics_excludes_retryable_error_rows(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    # the 10th: only the retryable 429 error (excluded); the 11th: its accepted retry
    assert (await _summary(acme, date(2026, 9, 10), date(2026, 9, 10)))["counts"][
        "publish_failed"
    ] == 0
    chain = (await _summary(acme, date(2026, 9, 10), date(2026, 9, 11)))["counts"]
    assert (chain["publish_succeeded"], chain["publish_failed"]) == (1, 0)
    # the 14th: the non-retryable error counts as failed
    assert (await _summary(acme, date(2026, 9, 14), date(2026, 9, 14)))["counts"][
        "publish_failed"
    ] == 1


async def test_analytics_partial_then_patch_two_attempts(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    chain = (await _summary(acme, date(2026, 9, 12), date(2026, 9, 13)))["counts"]
    assert (chain["publish_succeeded"], chain["publish_failed"]) == (1, 1)


async def test_analytics_excludes_attempt_2_runs(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    # the 8th holds an attempt-2 Rules Engine run and a `schema` run: neither counts
    day = (await _summary(acme, date(2026, 9, 8), date(2026, 9, 8)))["counts"]
    assert day["validation_first_pass_total"] == 0


async def test_analytics_excludes_pending_submissions(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    body = await _summary(acme, date(2026, 1, 1), date(2026, 12, 31))
    rows = world.conn.execute(
        "SELECT count(*) FROM listing_submissions WHERE tenant_id = %s AND outcome <> 'pending' "
        "AND NOT (outcome = 'error' AND error_report_json->>'retryable' = 'true')",
        [acme],
    ).fetchone()
    assert rows is not None
    seen = body["counts"]["publish_succeeded"] + body["counts"]["publish_failed"]
    assert seen == rows[0]  # every answered row, the pending one never


async def test_analytics_superseded_counts_success(world: AnalyticsWorld) -> None:
    acme = world.tenants["acme"]
    day = (await _summary(acme, date(2026, 9, 15), date(2026, 9, 15)))["counts"]
    assert (day["publish_succeeded"], day["publish_failed"]) == (1, 0)


@pytest.mark.parametrize("slug", ["acme", "bharat"])
async def test_by_marketplace_sums_to_counts(world: AnalyticsWorld, slug: str) -> None:
    body = await _summary(world.tenants[slug])
    for key, total in body["counts"].items():
        assert sum(row["counts"][key] for row in body["by_marketplace"]) == total
    assert body["listings_generated"] == body["counts"]["listings_generated"]


async def test_analytics_empty_tenant_all_null(world: AnalyticsWorld) -> None:
    from . import factories as f

    empty = f.tenant(world.conn)
    body = await _summary(empty)
    assert body["listings_generated"] == 0
    assert body["approval_rate"] is None
    assert body["validation_pass_rate"] is None
    assert body["publish_success_rate"] is None
    assert body["by_marketplace"] == []


async def test_analytics_unicode_free(world: AnalyticsWorld) -> None:
    """The result carries counts and rates only: no listing text, no ids."""
    body = await _summary(world.tenants["acme"])
    assert set(body) == {
        "from",
        "to",
        "listings_generated",
        "approval_rate",
        "validation_pass_rate",
        "publish_success_rate",
        "counts",
        "by_marketplace",
    }
    assert "A title" not in repr(body) and str(world.tenants["acme"]) not in repr(body)
