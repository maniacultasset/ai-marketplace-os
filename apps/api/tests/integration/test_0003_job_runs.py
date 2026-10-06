"""`job_runs` (DATABASE.md §5.4; Canon 0003, ADR-0004; SUPERSEDED.md:26): the 7 JobState values,
the 6 kinds, the `:946` CHECKs (attempt bounds, terminal ⇔ finished_at, running ⇒ lease, failure ⇒
report, no auto-publish), the live-job and idempotency uniques, the BEFORE INSERT queue trigger,
fillfactor 80 and the tenant-composite FKs (parent job included)."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from mkos_schemas.enums import JobKind, JobState, PipelineStage

from .conftest import expect_sqlstate
from .ops_support import Seed, job_run, seed_graph, seed_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

STATUSES = [s.value for s in JobState]
TERMINAL = ["succeeded", "partial", "failed", "cancelled", "dead_letter"]
LISTING_KINDS = ["listing_generate", "listing_publish", "listing_status_sync"]
FINISHED = "2026-10-05T00:00:00Z"
REPORT = {"errors": [{"error_code": "JOB_TIMEOUT"}]}


@pytest.fixture
def seed(db: Conn) -> Seed:
    return seed_graph(db)


def _status_columns(status: str) -> dict[str, Any]:
    """The columns a row in `status` needs to pass the other CHECKs."""
    columns: dict[str, Any] = {"status": status}
    if status in TERMINAL:
        columns["finished_at"] = FINISHED
    if status == "running":
        columns["lease_expires_at"] = FINISHED
        columns["attempt"] = 1
    if status in ("failed", "dead_letter"):
        columns["error_report_json"] = REPORT
    return columns


def _refused(db: Conn, constraint: str, tenant_id: UUID, **columns: Any) -> None:
    try:
        with db.transaction():
            job_run(db, tenant_id, **columns)
    except psycopg.Error as error:
        assert error.sqlstate == "23514", error
        assert error.diag.constraint_name == constraint, error.diag.constraint_name
    else:
        raise AssertionError(f"{columns} inserted")


def _fk_refused(db: Conn, constraint: str, tenant_id: UUID, **columns: Any) -> None:
    try:
        with db.transaction():
            job_run(db, tenant_id, **columns)
    except psycopg.Error as error:
        assert error.sqlstate == "23503", error
        assert error.diag.constraint_name == constraint, error.diag.constraint_name
    else:
        raise AssertionError(f"{columns} inserted")


def _check_literals(db: Conn, constraint: str) -> set[str]:
    row = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s "
        "AND conrelid = 'job_runs'::regclass",
        (constraint,),
    ).fetchone()
    assert row is not None, constraint
    return set(re.findall(r"'([a-z_]+)'::text", str(row[0])))


@pytest.mark.parametrize("status", STATUSES)
def test_job_runs_status_accepts_exactly_seven(db: Conn, seed: Seed, status: str) -> None:
    job_run(db, seed.a.tenant_id, **_status_columns(status))


@pytest.mark.parametrize("status", ["QUEUED", "blocked", "", "Queued", "done"])
def test_job_runs_status_rejects_others(db: Conn, seed: Seed, status: str) -> None:
    _refused(db, "ck_job_runs_status", seed.a.tenant_id, status=status)


def test_job_runs_status_defaults_queued(db: Conn, seed: Seed) -> None:
    job_id = job_run(db, seed.a.tenant_id)
    row = db.execute(
        "SELECT status, attempt, max_attempts, cost_reserved_usd, queued_at = now() "
        "FROM job_runs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row == ("queued", 0, 3, 0, True)


def test_job_runs_status_matches_jobstate(db: Conn) -> None:
    assert _check_literals(db, "ck_job_runs_status") == {s.value for s in JobState}
    assert _check_literals(db, "ck_job_runs_kind") == {k.value for k in JobKind}
    node = _check_literals(db, "ck_job_runs_node") - {"listing_generate"}
    assert node == {s.value for s in PipelineStage}
    assert len(node) == 12


@pytest.mark.parametrize("kind", ["listing_delete", "LISTING_GENERATE", "", "embed"])
def test_job_runs_kind_rejects_unknown(db: Conn, seed: Seed, kind: str) -> None:
    _refused(db, "ck_job_runs_kind", seed.a.tenant_id, kind=kind)


@pytest.mark.parametrize("entity_type", ["product", "", "LISTING"])
def test_job_runs_entity_type_vocab(db: Conn, seed: Seed, entity_type: str) -> None:
    _refused(db, "ck_job_runs_entity_type", seed.a.tenant_id, entity_type=entity_type)


def test_job_runs_node_generate_only(db: Conn, seed: Seed) -> None:
    _refused(db, "ck_job_runs_node", seed.a.tenant_id, node="not_a_stage")
    _refused(db, "ck_job_runs_node", seed.a.tenant_id, node="content_agent")
    job_run(
        db,
        seed.a.tenant_id,
        kind="listing_generate",
        listing_id=seed.a.listing_id,
        entity_type="listing",
        entity_id=seed.a.listing_id,
        node="content_agent",
    )


def test_job_runs_idempotency_same_key_refused(db: Conn, seed: Seed) -> None:
    job_run(db, seed.a.tenant_id, idempotency_key="k-1")
    try:
        with db.transaction():
            job_run(db, seed.a.tenant_id, idempotency_key="k-1")
    except psycopg.Error as error:
        assert error.sqlstate == "23505"
        assert error.diag.constraint_name == "ux_job_runs_idempotency_key"
    else:
        raise AssertionError("a second job with the same key inserted")


def test_job_runs_idempotency_null_keys_coexist(db: Conn, seed: Seed) -> None:
    job_run(db, seed.a.tenant_id)
    job_run(db, seed.a.tenant_id)
    job_run(db, seed.a.tenant_id, idempotency_key="k-2")
    job_run(db, seed.b.tenant_id, idempotency_key="k-2")


def test_job_runs_checks_attempt_over_max(db: Conn, seed: Seed) -> None:
    _refused(db, "ck_job_runs_attempt", seed.a.tenant_id, attempt=3, max_attempts=2)
    _refused(db, "ck_job_runs_attempt", seed.a.tenant_id, attempt=-1)
    job_run(db, seed.a.tenant_id, attempt=3, max_attempts=3)


def test_job_runs_checks_max_attempts_0(db: Conn, seed: Seed) -> None:
    _refused(db, "ck_job_runs_max_attempts", seed.a.tenant_id, max_attempts=0)


def test_job_runs_checks_max_attempts_4(db: Conn, seed: Seed) -> None:
    _refused(db, "ck_job_runs_max_attempts", seed.a.tenant_id, max_attempts=4)
    job_run(db, seed.a.tenant_id, max_attempts=1)


@pytest.mark.parametrize("status", TERMINAL)
def test_job_runs_checks_terminal_needs_finished(db: Conn, seed: Seed, status: str) -> None:
    columns = {**_status_columns(status), "finished_at": None}
    _refused(db, "ck_job_runs_finished", seed.a.tenant_id, **columns)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_job_runs_checks_finished_needs_terminal(db: Conn, seed: Seed, status: str) -> None:
    columns = {**_status_columns(status), "finished_at": FINISHED}
    _refused(db, "ck_job_runs_finished", seed.a.tenant_id, **columns)


def test_job_runs_checks_running_needs_lease(db: Conn, seed: Seed) -> None:
    _refused(db, "ck_job_runs_lease", seed.a.tenant_id, status="running", attempt=1)


@pytest.mark.parametrize("status", ["failed", "dead_letter"])
def test_job_runs_checks_failure_needs_report(db: Conn, seed: Seed, status: str) -> None:
    columns = {**_status_columns(status), "error_report_json": None}
    _refused(db, "ck_job_runs_failure_report", seed.a.tenant_id, **columns)


def test_job_runs_checks_no_auto_publish(db: Conn, seed: Seed) -> None:
    listing = {
        "listing_id": seed.a.listing_id,
        "entity_type": "listing",
        "entity_id": seed.a.listing_id,
    }
    _refused(
        db,
        "ck_job_runs_no_auto_publish",
        seed.a.tenant_id,
        kind="listing_publish",
        created_by=None,
        **listing,
    )
    job_run(db, seed.a.tenant_id, kind="listing_status_sync", created_by=None, **listing)
    job_run(db, seed.a.tenant_id, kind="listing_publish", created_by=seed.a.user_id, **listing)


@pytest.mark.parametrize("kind", LISTING_KINDS)
def test_job_runs_checks_listing_kinds_need_listing(db: Conn, seed: Seed, kind: str) -> None:
    _refused(
        db,
        "ck_job_runs_listing_kinds",
        seed.a.tenant_id,
        kind=kind,
        listing_id=None,
        created_by=seed.a.user_id,
    )


def test_job_runs_checks_one_live_per_listing_kind(db: Conn, seed: Seed) -> None:
    listing = {
        "kind": "listing_generate",
        "listing_id": seed.a.listing_id,
        "entity_type": "listing",
        "entity_id": seed.a.listing_id,
    }
    first = job_run(db, seed.a.tenant_id, **listing)
    for status in ("queued", "running"):
        try:
            with db.transaction():
                job_run(db, seed.a.tenant_id, **listing, **_status_columns(status))
        except psycopg.Error as error:
            assert error.sqlstate == "23505"
            assert error.diag.constraint_name == "ux_job_runs_live"
        else:
            raise AssertionError(f"a second live ({status}) job inserted")
    # another kind on the same listing, or the same kind on another listing, is not blocked
    job_run(db, seed.a.tenant_id, **{**listing, "kind": "listing_status_sync"})
    # queued -> cancelled: 0004's job guard allows only JobState edges
    db.execute(
        "UPDATE job_runs SET status = 'cancelled', finished_at = now() WHERE id = %s", (first,)
    )
    job_run(db, seed.a.tenant_id, **listing)


def test_job_runs_queue_trigger(db: Conn, seed: Seed) -> None:
    expected = {
        "listing_generate": "default",
        "listing_publish": "publish",
        "listing_status_sync": "publish",
        "bulk_import": "default",
        "image_compliance": "default",
        "brand_embed": "default",
    }
    assert set(expected) == {k.value for k in JobKind}
    for kind, queue in expected.items():
        # each kind on a tenant of its own, so the live-job unique never interferes
        side = seed_tenant(db)
        columns: dict[str, Any] = {"kind": kind, "created_by": side.user_id}
        if kind in LISTING_KINDS:
            columns.update(listing_id=side.listing_id, entity_type="listing")
        job_id = job_run(db, side.tenant_id, **columns)
        row = db.execute("SELECT queue FROM job_runs WHERE id = %s", (job_id,)).fetchone()
        assert row == (queue,), kind
    # a caller-supplied queue is overwritten
    forced = job_run(db, seed.a.tenant_id, kind="bulk_import", queue="publish")
    assert db.execute("SELECT queue FROM job_runs WHERE id = %s", (forced,)).fetchone() == (
        "default",
    )
    # the trigger covers INSERT; the vocabulary CHECK still guards an UPDATE
    expect_sqlstate(
        db,
        "23514",
        "UPDATE job_runs SET queue = 'bulk' WHERE id = %s",
        (forced,),
        constraint="ck_job_runs_queue",
    )


def test_job_runs_queue_trigger_catalog(db: Conn) -> None:
    rows = db.execute(
        "SELECT t.tgname, t.tgfoid::regprocedure::text, t.tgtype & 2 = 2, t.tgtype & 4 = 4, "
        "t.tgtype & 1 = 1 FROM pg_trigger t WHERE t.tgrelid = 'job_runs'::regclass "
        "AND NOT t.tgisinternal AND t.tgname <> 'job_runs_guard'"  # 0004 adds the edge guard
    ).fetchall()
    # BEFORE, INSERT, FOR EACH ROW
    assert rows == [("job_runs_set_queue", "job_runs_set_queue()", True, True, True)]
    generated = db.execute(
        "SELECT attgenerated FROM pg_attribute WHERE attrelid = 'job_runs'::regclass "
        "AND attname = 'queue'"
    ).fetchone()
    assert generated == ("",)


def test_job_runs_fillfactor_80(db: Conn) -> None:
    row = db.execute("SELECT reloptions FROM pg_class WHERE oid = 'job_runs'::regclass").fetchone()
    assert row == (["fillfactor=80"],)


def test_job_runs_no_created_at(db: Conn) -> None:
    rows = db.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'job_runs'::regclass "
        "AND attname IN ('created_at', 'updated_at', 'arq_job_id', 'queue_job_id')"
    ).fetchall()
    assert rows == [("queue_job_id",)]


def test_job_runs_indexes(db: Conn) -> None:
    rows: dict[str, str] = dict(
        db.execute(
            "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indrelid = 'job_runs'::regclass"
        ).fetchall()
    )
    expected = {
        "ux_job_runs_live": "UNIQUE INDEX ux_job_runs_live ON public.job_runs USING btree "
        "(tenant_id, listing_id, kind) WHERE (status = ANY (ARRAY['queued'::text, 'running'::text]))",
        "uq_job_runs_parent_job_id": "UNIQUE INDEX uq_job_runs_parent_job_id ON public.job_runs "
        "USING btree (parent_job_id)",
        "ix_job_runs_lease": "INDEX ix_job_runs_lease ON public.job_runs USING btree "
        "(lease_expires_at) WHERE (status = 'running'::text)",
        "ix_job_runs_dead_letter_tenant": "INDEX ix_job_runs_dead_letter_tenant ON public.job_runs "
        "USING btree (tenant_id, finished_at DESC) WHERE (status = 'dead_letter'::text)",
        "ix_job_runs_dead_letter": "INDEX ix_job_runs_dead_letter ON public.job_runs USING btree "
        "(finished_at DESC) WHERE (status = 'dead_letter'::text)",
        "ix_job_runs_batch": "INDEX ix_job_runs_batch ON public.job_runs USING btree "
        "(tenant_id, batch_id) WHERE (batch_id IS NOT NULL)",
        "ix_job_runs_recent": "INDEX ix_job_runs_recent ON public.job_runs USING btree "
        "(tenant_id, queued_at DESC, id DESC)",
        "ix_job_runs_entity": "INDEX ix_job_runs_entity ON public.job_runs USING btree "
        "(tenant_id, entity_type, entity_id)",
        "ux_job_runs_idempotency_key": "UNIQUE INDEX ux_job_runs_idempotency_key ON "
        "public.job_runs USING btree (tenant_id, idempotency_key) "
        "WHERE (idempotency_key IS NOT NULL)",
    }
    for name, definition in expected.items():
        assert rows[name] == f"CREATE {definition}", name


def test_job_runs_cross_tenant_listing_refused(db: Conn, seed: Seed) -> None:
    _fk_refused(
        db,
        "fk_job_runs_listing_id",
        seed.b.tenant_id,
        kind="listing_generate",
        listing_id=seed.a.listing_id,
        entity_type="listing",
        entity_id=seed.a.listing_id,
    )


def test_job_runs_cross_tenant_batch_refused(db: Conn, seed: Seed) -> None:
    _fk_refused(db, "fk_job_runs_batch_id", seed.b.tenant_id, batch_id=seed.a.batch_id)
    job_run(db, seed.a.tenant_id, batch_id=seed.a.batch_id)


def test_ops_cross_tenant_fk_job_parent(db: Conn, seed: Seed) -> None:
    parent = job_run(db, seed.a.tenant_id)
    _fk_refused(db, "fk_job_runs_parent_job_id", seed.b.tenant_id, parent_job_id=parent)
    _fk_refused(db, "fk_job_runs_parent_job_id", seed.a.tenant_id, parent_job_id=uuid4())
    job_run(db, seed.a.tenant_id, parent_job_id=parent)
    # one requeue per job (UNIQUE (parent_job_id))
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id, parent_job_id) VALUES (%s, 'bulk_import', 'import', %s, '{}', %s, %s)",
        (seed.a.tenant_id, uuid4(), uuid4(), parent),
        constraint="uq_job_runs_parent_job_id",
    )


def test_ops_cross_tenant_fk_job_created_by(db: Conn, seed: Seed) -> None:
    _fk_refused(db, "fk_job_runs_created_by", seed.a.tenant_id, created_by=seed.b.user_id)
    job_run(db, seed.a.tenant_id, created_by=seed.a.user_id)


def test_job_runs_tenant_delete_cascades(db: Conn, seed: Seed) -> None:
    parent = job_run(db, seed.a.tenant_id)
    job_run(db, seed.a.tenant_id, parent_job_id=parent)
    job_run(db, seed.b.tenant_id)
    db.execute("DELETE FROM tenants WHERE id = %s", (seed.a.tenant_id,))
    rows = db.execute("SELECT tenant_id FROM job_runs").fetchall()
    assert rows == [(seed.b.tenant_id,)]
