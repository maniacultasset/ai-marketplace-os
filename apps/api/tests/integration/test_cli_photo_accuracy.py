"""SI10 T18 (AC16): `python -m app.cli ops photo-accuracy --days N` sums the seller reviews of
photo-read values across tenants, read-only, and prints no tenant, product or value."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app import cli

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SECRET_VALUE = "saffron-zari-7731"  # a value that must never be printed


def _photo(value: str, confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "source": "ai",
        "captured_via": "agent",
        "confidence": confidence,
        "evidence_ref": "image:abc",
    }


def _review(
    jw: JobWorld, slug: str, key: str, after: Any, *, at: datetime = NOW, value: str = "red"
) -> None:
    f.audit(
        jw.conn,
        jw.tenant(slug),
        actor_type="user",
        actor_user_id=jw.world.user(f"owner@{slug}.test"),
        action="product.fact_updated",
        entity_type="product",
        before=json.dumps({"attributes": {key: _photo(value, 0.8)}}),
        after=json.dumps({"attributes": {key: after}}),
        occurred_at=at,
    )


async def test_per_key_counts_summed_across_tenants(
    jobs_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    confirmed = {**_photo("red", 0.8), "confirmed_at": "2026-11-07T11:00:00Z"}
    _review(jobs_db, "acme", "color", confirmed)
    _review(jobs_db, "bharat", "color", None)
    _review(jobs_db, "bharat", "sleeve_type", {"value": SECRET_VALUE, "source": "seller"})
    _review(jobs_db, "acme", "color", None, at=NOW - timedelta(days=3))  # outside --days 1
    rows_before = jobs_db.conn.execute("SELECT count(*) FROM audit_log").fetchone()
    assert await cli.run_photo_accuracy(1, now=NOW) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line.split() for line in out.splitlines()[1:-1]}
    assert lines["color"][1:4] == ["1", "1", "0"]
    assert lines["sleeve_type"][1:4] == ["0", "0", "1"]
    assert "2 key(s) over 1 day(s)" in out
    for hidden in ("acme", "bharat", "Acme", "Bharat", SECRET_VALUE, "red"):
        assert hidden not in out
    assert jobs_db.conn.execute("SELECT count(*) FROM audit_log").fetchone() == rows_before
    assert await cli.run_photo_accuracy(7, now=NOW) == 0
    week = capsys.readouterr().out
    assert {line.split()[0]: line.split() for line in week.splitlines()[1:-1]}["color"][1:4] == [
        "1",
        "2",
        "0",
    ]


@pytest.mark.parametrize("days", ["0", "366", "x"])
def test_days_out_of_range_refused(days: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ops", "photo-accuracy", "--days", days])


def test_days_default_30() -> None:
    assert cli.build_parser().parse_args(["ops", "photo-accuracy"]).days == 30
