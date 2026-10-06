"""0004/08_job_guards (DATABASE.md §5.4 job_runs :947, §6.2 :1465-1487): JobState edges, terminal
rows immutable, DELETE allowed."""

from __future__ import annotations

import itertools
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate
from .ops_support import job_run

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

STATES = ("queued", "running", "succeeded", "partial", "failed", "cancelled", "dead_letter")
TERMINAL = ("succeeded", "partial", "failed", "cancelled", "dead_letter")
# DATABASE.md §6.2: the claim, cancel before start, the five outcomes of a run, and the
# retry/reaper edges back to queued or to the dead-letter queue.
EDGES = (
    ("queued", "running"),
    ("queued", "cancelled"),
    ("running", "succeeded"),
    ("running", "partial"),
    ("running", "queued"),
    ("running", "failed"),
    ("running", "dead_letter"),
    ("running", "cancelled"),
)
REFUSED = [(a, b) for a, b in itertools.product(STATES, STATES) if a != b and (a, b) not in EDGES]


def _columns(status: str) -> dict[str, Any]:
    """What the job_runs CHECKs need for a row in `status`."""
    values: dict[str, Any] = {"status": status, "finished_at": None, "lease_expires_at": None}
    if status == "running":
        values.update(started_at="2026-10-06T00:00:00Z", lease_expires_at="2099-01-01T00:00:00Z")
    if status in TERMINAL:
        values["finished_at"] = "2026-10-06T01:00:00Z"
    values["error_report_json"] = (
        Jsonb({"errors": []}) if status in ("failed", "dead_letter") else None
    )
    return values


def _job(db: Conn, status: str) -> UUID:
    columns = {k: v for k, v in _columns(status).items() if v is not None}
    columns.pop("error_report_json", None)
    if status in ("failed", "dead_letter"):
        columns["error_report_json"] = {"errors": []}
    return job_run(db, f.tenant(db), **columns)


def _move(status: str) -> tuple[str, list[Any]]:
    values = _columns(status)
    sets = ", ".join(f"{name} = %s" for name in values)
    return f"UPDATE job_runs SET {sets} WHERE id = %s", list(values.values())  # noqa: S608


@pytest.mark.parametrize(("src", "dst"), EDGES, ids=lambda s: str(s))
def test_job_edges_allowed(db: Conn, src: str, dst: str) -> None:
    job = _job(db, src)
    query, params = _move(dst)
    db.execute(query, [*params, job])
    assert db.execute("SELECT status FROM job_runs WHERE id = %s", (job,)).fetchone() == (dst,)


def test_job_edges_refused(db: Conn) -> None:
    """The exhaustive remainder of the 7 by 7 matrix (no self-pairs): 34 pairs, each GRD01."""
    assert len(REFUSED) == 34
    for src, dst in REFUSED:
        job = _job(db, src)
        query, params = _move(dst)
        expect_sqlstate(db, "GRD01", query, [*params, job])


@pytest.mark.parametrize("status", TERMINAL)
def test_job_terminal_immutable(db: Conn, status: str) -> None:
    job = _job(db, status)
    expect_sqlstate(db, "GRD01", "UPDATE job_runs SET request_id = 'x' WHERE id = %s", (job,))
    expect_sqlstate(db, "GRD01", "UPDATE job_runs SET result_json = '{}' WHERE id = %s", (job,))


def test_job_non_terminal_other_columns_change(db: Conn) -> None:
    job = _job(db, "running")
    db.execute("UPDATE job_runs SET node = NULL, request_id = 'r' WHERE id = %s", (job,))
    db.execute(
        "UPDATE job_runs SET lease_expires_at = lease_expires_at + interval '1 minute' "
        "WHERE id = %s",
        (job,),
    )


@pytest.mark.parametrize("status", TERMINAL)
def test_job_terminal_delete_allowed(db: Conn, status: str) -> None:
    job = _job(db, status)
    db.execute("SET LOCAL ROLE app_rw")
    db.execute("DELETE FROM job_runs WHERE id = %s", (job,))
    db.execute("RESET ROLE")
    assert db.execute("SELECT count(*) FROM job_runs WHERE id = %s", (job,)).fetchone() == (0,)


@pytest.mark.parametrize("src", [s for s in STATES if s not in ("queued", "running")])
def test_job_claim_only_from_queued(db: Conn, src: str) -> None:
    job = _job(db, src)
    query, params = _move("running")
    expect_sqlstate(db, "GRD01", query, [*params, job])
