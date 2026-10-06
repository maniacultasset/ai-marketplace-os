"""Attacks on revisions 0003/0003b by the step's tester: the one-DELETE tenant purge and the
documented purge order over a full 0002 + 0003 graph, writes as `app_rw`, the job lifecycle the
§5.4 ops notes write against the CHECKs (claim, duplicate delivery, reaper, retry-cap exhaustion,
requeue), the publish statement order and supersede, a retry chain with the partial-publish retry
payload, and the vendored procrastinate SQL compared object by object with a reference install
of the pinned library's own SQL."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from procrastinate.schema import SchemaManager
from psycopg import sql
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_migrator, disable_0004_guard, set_gucs
from .ops_support import (
    TenantSeed,
    field_result,
    idempotency_key,
    import_row,
    import_run,
    job_run,
    make_publishable,
    oauth_state,
    refresh_token,
    seed_tenant,
    submission,
)
from .test_0002_attacks import PURGE_ORDER as PURGE_ORDER_0002
from .test_0002_attacks import TABLES_0002, _graph

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


@pytest.fixture(autouse=True)
def _ls_guard_off(request: pytest.FixtureRequest) -> None:
    """These suites test 0003's CHECKs, keys and FKs on submissions written in any order;
    0004's `ls_guard` (proven by test_0004_ls_guard.py) is off in their rolled-back transaction."""
    if "db" in request.fixturenames:
        disable_0004_guard(request.getfixturevalue("db"), "listing_submissions", "ls_guard")


Conn = psycopg.Connection[tuple[Any, ...]]

TENANT_TABLES_0003 = (
    "job_runs",
    "imports",
    "import_rows",
    "idempotency_keys",
    "refresh_tokens",
    "oauth_states",
    "listing_submissions",
    "publish_field_results",
)
# DATABASE.md §9 purge_tenant(t, 'data'), children first: publish_field_results →
# listing_submissions; listing_submissions and imports before job_runs; job_runs before batches
# and listings (job_runs.listing_id, batch_id). Then the 0002 order.
PURGE_ORDER_0003 = (
    "publish_field_results",
    "listing_submissions",
    "import_rows",
    "imports",
    "idempotency_keys",
    "refresh_tokens",
    "oauth_states",
    "job_runs",
)
REPORT = {"errors": [{"error_code": "JOB_TIMEOUT"}]}
SUBMISSION_REPORT = {"errors": [{"error_code": "SUBMISSION_NOT_APPLIED"}]}


def _ops_graph(db: Conn, side: TenantSeed, marketplace: str = "amazon") -> None:
    """At least one row in every 0003 tenant table, wired through every FK kind: a requeue
    chain, a publish job and its submission with per-field results, an import whose row points
    at a product, a rotated refresh token, an OAuth state and an idempotency key."""
    parent = job_run(db, side.tenant_id, batch_id=side.batch_id)
    job_run(db, side.tenant_id, parent_job_id=parent, batch_id=side.batch_id)
    status = db.execute("SELECT status FROM listings WHERE id = %s", (side.listing_id,))
    if status.fetchone() != ("approved",):
        make_publishable(db, side)  # 0004's publish gate
    publish = job_run(
        db,
        side.tenant_id,
        kind="listing_publish",
        entity_type="listing",
        entity_id=side.listing_id,
        listing_id=side.listing_id,
        created_by=side.user_id,
    )
    submission_id = submission(db, side, job_id=publish, marketplace=marketplace)
    field_result(db, side, submission_id)
    field_result(
        db, side, submission_id, field="content.bullets", status="failed", error_json={"e": 1}
    )
    import_id = import_run(db, side)
    import_row(db, side.tenant_id, import_id, product_id=side.product_id)
    import_row(db, side.tenant_id, import_id)
    newer = refresh_token(db, side)
    refresh_token(db, side, replaced_by=newer, revoked_at="2026-10-05T00:00:00Z")
    oauth_state(db, side, marketplace=marketplace)
    idempotency_key(db, side)


def _counts(db: Conn, tenant_id: UUID, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        row = db.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
            (tenant_id,),
        ).fetchone()
        assert row is not None
        counts[table] = int(row[0])
    return counts


ALL_TENANT_TABLES = (*TABLES_0002, *TENANT_TABLES_0003)


def _full_tenant(db: Conn) -> TenantSeed:
    """The whole 0002 graph (its Amazon account included) plus the 0003 graph on a Flipkart
    listing of the same tenant (one live account per marketplace)."""
    tenant_id = f.tenant(db)
    _graph(db, tenant_id)
    user_id = f.member(db, tenant_id)
    product_id = f.product(db, tenant_id)
    account_id = f.marketplace_account(
        db, tenant_id, marketplace="flipkart", connected_by_user_id=user_id
    )
    listing_id = f.listing(
        db, tenant_id, product_id, marketplace="flipkart", marketplace_account_id=account_id
    )
    version_id = f.version(db, tenant_id, listing_id, generated_by_user_id=user_id)
    item_id = f.listing_item(db, tenant_id, listing_id, product_id, marketplace="flipkart")
    batch_id = f.batch(db, tenant_id, created_by_user_id=user_id)
    side = TenantSeed(
        tenant_id, user_id, product_id, account_id, listing_id, version_id, item_id, batch_id
    )
    _ops_graph(db, side, marketplace="flipkart")
    return side


def test_tenant_delete_cascades_whole_0003_graph(db: Conn) -> None:
    """DATABASE.md §1 "tenants(id) cascades, so the final purge is one DELETE", with the 0003
    NO ACTION links (job_runs → listings/batches/self, submissions → job_runs, imports →
    job_runs) and the SET NULL (col) exceptions all firing inside the one cascade."""
    a, b = _full_tenant(db), _full_tenant(db)
    before_b = _counts(db, b.tenant_id, ALL_TENANT_TABLES)
    assert all(n > 0 for n in _counts(db, a.tenant_id, ALL_TENANT_TABLES).values())
    with as_migrator(db):  # 0004: review_decisions and validation_runs are append-only
        set_gucs(db, purge_tenant=a.tenant_id)
        db.execute("DELETE FROM tenants WHERE id = %s", (a.tenant_id,))
    assert set(_counts(db, a.tenant_id, ALL_TENANT_TABLES).values()) == {0}
    assert _counts(db, b.tenant_id, ALL_TENANT_TABLES) == before_b


def test_purge_order_over_full_0003_graph(db: Conn) -> None:
    """DATABASE.md §9: the documented children-first order, the 0003 tables ahead of the 0002
    order, respects every FK (no NO ACTION violation) and leaves the other tenant whole."""
    a, b = _full_tenant(db), _full_tenant(db)
    before_b = _counts(db, b.tenant_id, ALL_TENANT_TABLES)
    with as_migrator(db), db.transaction():  # 0004: append-only tables need the purge GUC
        set_gucs(db, purge_tenant=a.tenant_id)
        db.execute("SET CONSTRAINTS ALL DEFERRED")
        for table in (*PURGE_ORDER_0003, *PURGE_ORDER_0002):
            db.execute(
                sql.SQL("DELETE FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
                (a.tenant_id,),
            )
        db.execute("DELETE FROM tenants WHERE id = %s", (a.tenant_id,))
    assert set(_counts(db, a.tenant_id, ALL_TENANT_TABLES).values()) == {0}
    assert _counts(db, b.tenant_id, ALL_TENANT_TABLES) == before_b


def test_purge_order_job_runs_after_batches_fails(db: Conn) -> None:
    """The order matters: batches before job_runs trips the NO ACTION `job_runs.batch_id`
    ("review_decisions and job_runs before batches", DATABASE.md §9)."""
    side = seed_tenant(db)
    _ops_graph(db, side)
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM batches WHERE tenant_id = %s",
        (side.tenant_id,),
        constraint="fk_job_runs_batch_id",
    )


def test_app_rw_writes_every_0003_tenant_table(db: Conn) -> None:
    """The runtime role writes each new tenant table and the counters through the 0000 default
    privileges (no 42501 before the 0004 column grants)."""
    side = seed_tenant(db)
    make_publishable(db, side)  # 0004's publish gate (its set-up needs the superuser)
    set_gucs(db, tenant_id=side.tenant_id)  # 0005: the unit of work's GUC
    db.execute("SET LOCAL ROLE app_rw")
    _ops_graph(db, side)
    row = db.execute(
        "INSERT INTO rate_limit_counters (bucket_key, window_start, n) "
        "VALUES (%s, date_trunc('minute', now()), 1) "
        "ON CONFLICT (bucket_key, window_start) DO UPDATE SET n = rate_limit_counters.n + 1 "
        "RETURNING n",
        (f"t:{side.tenant_id}",),
    ).fetchone()
    assert row == (1,)
    counts = _counts(db, side.tenant_id, TENANT_TABLES_0003)
    assert all(n > 0 for n in counts.values()), counts


# ---- job lifecycle (DATABASE.md §5.4 "Ops notes") against the CHECKs ----

CLAIM = (
    "UPDATE job_runs SET status = 'running', attempt = attempt + 1, started_at = now(), "
    "lease_expires_at = now() - interval '1 second' "
    "WHERE id = %s AND status = 'queued' AND attempt < max_attempts RETURNING attempt"
)
REAP_SELECT = "SELECT id, attempt, max_attempts FROM job_runs "
REAP_SELECT += "WHERE status = 'running' AND lease_expires_at < now() AND id = %s"
REAP_REQUEUE = (
    "UPDATE job_runs SET status = 'queued', lease_expires_at = NULL, queue_job_id = %s "
    "WHERE id = %s AND status = 'running'"
)
REAP_DEAD = (
    "UPDATE job_runs SET status = 'dead_letter', lease_expires_at = NULL, finished_at = now(), "
    "error_code = 'JOB_TIMEOUT', error_report_json = %s WHERE id = %s AND status = 'running'"
)


def test_job_lifecycle_claim_duplicate_reaper_dead_letter(db: Conn) -> None:
    """Claim CAS, a duplicate delivery (0 rows), the reaper's requeue while attempts are left,
    a claim refused at the cap, and the reaper's dead_letter with JOB_TIMEOUT."""
    side = seed_tenant(db)
    job = job_run(db, side.tenant_id, max_attempts=3, queue_job_id="1")
    for attempt in (1, 2, 3):
        assert db.execute(CLAIM, (job,)).fetchall() == [(attempt,)]
        assert db.execute(CLAIM, (job,)).fetchall() == []  # duplicate delivery: dropped
        reaped = db.execute(REAP_SELECT, (job,)).fetchone()
        assert reaped is not None and reaped[1] == attempt
        if reaped[1] < reaped[2]:
            db.execute(REAP_REQUEUE, (str(attempt + 1), job))
            # a requeued job is not claimable past the cap and holds no finish stamp
            assert db.execute(
                "SELECT status, finished_at, lease_expires_at FROM job_runs WHERE id = %s", (job,)
            ).fetchone() == ("queued", None, None)
        else:
            db.execute(REAP_DEAD, (Jsonb(REPORT), job))
    assert db.execute(
        "SELECT status, attempt, error_code FROM job_runs WHERE id = %s", (job,)
    ).fetchone() == ("dead_letter", 3, "JOB_TIMEOUT")
    # the cap: even forced back to queued, attempt 3 of 3 is never claimed again
    assert db.execute(CLAIM, (job,)).fetchall() == []


def test_job_dead_letter_needs_report_and_finish(db: Conn) -> None:
    side = seed_tenant(db)
    job = job_run(db, side.tenant_id, max_attempts=1)
    assert db.execute(CLAIM, (job,)).fetchall() == [(1,)]
    expect_sqlstate(
        db,
        "23514",
        "UPDATE job_runs SET status = 'dead_letter', finished_at = now() WHERE id = %s",
        (job,),
        constraint="ck_job_runs_failure_report",
    )
    expect_sqlstate(
        db,
        "23514",
        "UPDATE job_runs SET status = 'dead_letter', error_report_json = %s WHERE id = %s",
        (Jsonb(REPORT), job),
        constraint="ck_job_runs_finished",
    )
    # a running job never loses its lease while still running
    expect_sqlstate(
        db,
        "23514",
        "UPDATE job_runs SET lease_expires_at = NULL WHERE id = %s",
        (job,),
        constraint="ck_job_runs_lease",
    )


def test_job_requeue_inserts_one_child_in_tenant(db: Conn) -> None:
    """Requeue always inserts a new row (SUPERSEDED.md:73) copying created_by: one child per
    parent (`UNIQUE (parent_job_id)`), and the child cannot name another tenant's job."""
    a, b = seed_tenant(db), seed_tenant(db)
    parent = job_run(
        db,
        a.tenant_id,
        status="dead_letter",
        finished_at="2026-10-05T00:00:00Z",
        error_report_json=REPORT,
        created_by=a.user_id,
    )
    job_run(db, a.tenant_id, parent_job_id=parent, created_by=a.user_id)
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id, parent_job_id) VALUES (%s, 'bulk_import', 'import', %s, '{}', %s, %s)",
        (a.tenant_id, uuid4(), uuid4(), parent),
        constraint="uq_job_runs_parent_job_id",
    )
    childless = job_run(db, a.tenant_id)
    expect_sqlstate(
        db,
        "23503",
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id, parent_job_id) VALUES (%s, 'bulk_import', 'import', %s, '{}', %s, %s)",
        (b.tenant_id, uuid4(), uuid4(), childless),
        constraint="fk_job_runs_parent_job_id",
    )


def _publish_job(db: Conn, side: TenantSeed, **columns: Any) -> UUID:
    return job_run(
        db,
        side.tenant_id,
        kind="listing_publish",
        entity_type="listing",
        entity_id=side.listing_id,
        listing_id=side.listing_id,
        created_by=side.user_id,
        **columns,
    )


def test_publish_statement_order_auto_resubmit(db: Conn) -> None:
    """DATABASE.md §5.4: an auto_resubmit from a running publish job must write that job's
    terminal status first; inserting the new job first is refused by the live-job unique. A
    status sync for the same listing is a different kind, lives beside it, and shares the
    `publish` queue."""
    side = seed_tenant(db)
    make_publishable(db, side)  # 0004's publish gate
    running = _publish_job(db, side, status="running", lease_expires_at="2099-01-01T00:00:00Z")
    sync = job_run(
        db,
        side.tenant_id,
        kind="listing_status_sync",
        entity_type="listing",
        entity_id=side.listing_id,
        listing_id=side.listing_id,
    )
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, listing_id, "
        "payload_json, correlation_id, created_by) "
        "VALUES (%s, 'listing_publish', 'listing', %s, %s, '{}', %s, %s)",
        (side.tenant_id, side.listing_id, side.listing_id, uuid4(), side.user_id),
        constraint="ux_job_runs_live",
    )
    db.execute(
        "UPDATE job_runs SET status = 'failed', finished_at = now(), lease_expires_at = NULL, "
        "error_report_json = %s WHERE id = %s",
        (Jsonb(SUBMISSION_REPORT), running),
    )
    resubmit = _publish_job(db, side)
    queues: dict[UUID, str] = dict(
        db.execute(
            "SELECT id, queue FROM job_runs WHERE id IN (%s, %s, %s)", (running, sync, resubmit)
        ).fetchall()
    )
    assert queues == {running: "publish", sync: "publish", resubmit: "publish"}


def test_supersede_open_accepted_submission_in_same_transaction(db: Conn) -> None:
    """DATABASE.md §5.3: a newer approved version's attempt 1 first moves the older version's
    open `accepted` row to `superseded`; the INSERT alone is refused (409 SUBMISSION_IN_FLIGHT)."""
    side = seed_tenant(db)
    old = submission(db, side, outcome="accepted", responded_at="2026-10-05T00:00:00Z")
    v2 = f.version(db, side.tenant_id, side.listing_id, generated_by_user_id=side.user_id)
    make_publishable(db, side, v2)  # 0004's publish gate: the newer version is the approved one
    newer = {
        "listing_version_id": v2,
        "job_id": _publish_job(db, side, status="running", lease_expires_at="2099-01-01T00:00:00Z"),
    }
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        submission(db, side, **newer)
    assert caught.value.diag.constraint_name == "ux_ls_open"
    with db.transaction():
        db.execute(
            "UPDATE listing_submissions SET outcome = 'superseded', resolved_at = now() "
            "WHERE id = %s",
            (old,),
        )
        submission(db, side, **newer)
    open_rows = db.execute(
        "SELECT listing_version_id FROM listing_submissions "
        "WHERE listing_id = %s AND outcome IN ('pending', 'accepted')",
        (side.listing_id,),
    ).fetchall()
    assert open_rows == [(v2,)]


def _resolve(db: Conn, submission_id: UUID, outcome: str, code: str | None = None) -> None:
    db.execute(
        "UPDATE listing_submissions SET outcome = %s, error_code = %s, error_report_json = %s, "
        "responded_at = now(), resolved_at = now() WHERE id = %s",
        (outcome, code, Jsonb(SUBMISSION_REPORT), submission_id),
    )


RETRY_PAYLOAD = """
SELECT listing_item_id, field FROM (
  SELECT DISTINCT ON (listing_item_id, field) listing_item_id, field, status
    FROM publish_field_results
   WHERE tenant_id = %s AND listing_id = %s AND listing_version_id = %s
   ORDER BY listing_item_id, field, created_at DESC
) latest WHERE status = 'failed' ORDER BY field
"""


def test_retry_chain_partial_publish_then_exhausted(db: Conn) -> None:
    """Inv. 9 and 10 on the schema: three attempts chained by previous_submission_id, the
    retry payload (latest result per field, then filtered to failed) resends only the fields
    still failing, and a 4th attempt is refused."""
    side = seed_tenant(db)
    first = submission(db, side)
    field_result(db, side, first, field="content.title", status="failed", error_json={"e": 1})
    field_result(db, side, first, field="content.bullets", status="failed", error_json={"e": 2})
    field_result(db, side, first, field="content.description")
    _resolve(db, first, "partially_accepted")
    second = submission(
        db,
        side,
        attempt=2,
        operation="patch",
        field_paths=["content.title", "content.bullets"],
        previous_submission_id=first,
        retry_justification="changed_input",
    )
    # rows of one transaction share now(): stamp the second attempt's results later
    field_result(db, side, second, created_at="2099-01-01T00:00:00Z")
    field_result(
        db,
        side,
        second,
        field="content.bullets",
        status="failed",
        error_json={"e": 3},
        created_at="2099-01-01T00:00:00Z",
    )
    _resolve(db, second, "partially_accepted")
    payload = db.execute(
        RETRY_PAYLOAD, (side.tenant_id, side.listing_id, side.version_id)
    ).fetchall()
    assert payload == [(side.item_id, "content.bullets")]
    third = submission(
        db,
        side,
        attempt=3,
        operation="patch",
        field_paths=["content.bullets"],
        previous_submission_id=second,
        retry_justification="changed_input",
    )
    _resolve(db, third, "error", "SUBMISSION_NOT_APPLIED")
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        submission(
            db,
            side,
            attempt=4,
            operation="patch",
            field_paths=["content.bullets"],
            previous_submission_id=third,
            retry_justification="retryable_condition",
        )
    assert caught.value.diag.constraint_name == "ck_listing_submissions_attempt"


def test_submission_previous_must_be_own_listing_version(db: Conn) -> None:
    """The previous-attempt FK pins (id, listing, version): a retry cannot chain onto another
    tenant's submission, nor onto its own listing's other version."""
    a, b = seed_tenant(db), seed_tenant(db)
    theirs = submission(db, b)
    _resolve(db, theirs, "rejected")
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as caught, db.transaction():
        submission(
            db,
            a,
            attempt=2,
            previous_submission_id=theirs,
            retry_justification="changed_input",
        )
    assert caught.value.diag.constraint_name == "fk_listing_submissions_previous_submission_id"


# ---- 0003b: what was applied equals a reference install of the library's own SQL ----

_SCHEMA_PREFIX = re.compile(r"\b(?:public|lib_ref)\.")


def _procrastinate_catalog(db: Conn, schema: str) -> dict[str, list[tuple[str, ...]]]:
    def strip(rows: list[tuple[Any, ...]]) -> list[tuple[str, ...]]:
        return sorted(tuple(_SCHEMA_PREFIX.sub("", str(v)) for v in row) for row in rows)

    ns = (schema,)
    return {
        "functions": strip(
            db.execute(
                "SELECT p.proname, pg_get_function_identity_arguments(p.oid), "
                "pg_get_function_result(p.oid), p.prosrc, p.prosecdef, p.provolatile "
                "FROM pg_proc p WHERE p.pronamespace = %s::regnamespace "
                "AND p.proname LIKE 'procrastinate%%'",
                ns,
            ).fetchall()
        ),
        "columns": strip(
            db.execute(
                "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
                "a.attnotnull, coalesce(pg_get_expr(d.adbin, d.adrelid), '') "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE c.relnamespace = %s::regnamespace AND c.relkind IN ('r', 'c') "
                "AND c.relname LIKE 'procrastinate%%' AND a.attnum > 0 AND NOT a.attisdropped",
                ns,
            ).fetchall()
        ),
        "constraints": strip(
            db.execute(
                "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid) FROM pg_constraint k "
                "JOIN pg_class c ON c.oid = k.conrelid WHERE c.relnamespace = %s::regnamespace "
                "AND c.relname LIKE 'procrastinate%%'",
                ns,
            ).fetchall()
        ),
        "indexes": strip(
            db.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
                "AND tablename LIKE 'procrastinate%%'",
                ns,
            ).fetchall()
        ),
        "triggers": strip(
            db.execute(
                "SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid WHERE c.relnamespace = %s::regnamespace "
                "AND c.relname LIKE 'procrastinate%%' AND NOT t.tgisinternal",
                ns,
            ).fetchall()
        ),
        "enums": strip(
            db.execute(
                "SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) "
                "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE t.typnamespace = %s::regnamespace AND t.typname LIKE 'procrastinate%%' "
                "GROUP BY t.typname",
                ns,
            ).fetchall()
        ),
    }


def test_procrastinate_applied_objects_equal_library_install(db: Conn) -> None:
    """AC 11 on what 0003b applied, not only on the vendored file: every procrastinate function
    body, column, default, constraint, index, trigger and enum in `public` equals what the pinned
    library's own `SchemaManager.get_schema()` creates when run directly (no SQLAlchemy text()
    processing) into a scratch schema."""
    db.execute("CREATE SCHEMA lib_ref")
    db.execute("SET LOCAL search_path = lib_ref")
    db.execute(SchemaManager.get_schema().encode("utf-8"))
    db.execute("SET LOCAL search_path = public")
    applied = _procrastinate_catalog(db, "public")
    reference = _procrastinate_catalog(db, "lib_ref")
    assert len(reference["functions"]) == 18
    for kind in applied:
        assert applied[kind] == reference[kind], kind
