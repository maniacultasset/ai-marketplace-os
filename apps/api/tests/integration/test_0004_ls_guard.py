"""0004/10_ls_guard (DATABASE.md §5.3 listing_submissions :873-876): the submission ledger's
backstop for invariants 1, 3, 9 and 10."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate, head_database
from .integrity_support import (
    Flow,
    approved_flow,
    flow,
    force_state,
    next_version,
    queued_job,
    scalar,
    set_gucs,
    submission,
)

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_COLUMNS = (
    "tenant_id, listing_id, listing_version_id, marketplace, marketplace_account_id, job_id, "
    "attempt, operation, payload_sha256, payload_storage_key, facts_digest, idempotency_key, "
    "requested_by_user_id, actor_type, retry_justification, previous_submission_id, "
    "corrections_json, field_paths"
)


def _publishing(db: Conn) -> tuple[Flow, UUID]:
    """A publishing listing (v1 approved, its approve decision's digest) and its publish job."""
    fl = flow(db)
    set_gucs(db, user_id=fl.operator)
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "approved")
    job = queued_job(db, fl, "listing_publish")
    force_state(db, fl.listing_id, "publishing")
    return fl, job


def _fail(db: Conn, sub: UUID, outcome: str = "error", **report: Any) -> None:
    db.execute(
        "UPDATE listing_submissions SET outcome = %s, error_code = 'RATE_LIMITED', "
        "error_report_json = %s, responded_at = now(), resolved_at = now() WHERE id = %s",
        (outcome, Jsonb({"status": "failed", "errors": [], **report}), sub),
    )


def _insert_sql(**columns: Any) -> tuple[str, list[Any]]:
    names = list(columns)
    values = [
        Jsonb(v) if isinstance(v, (dict, list)) and k == "corrections_json" else v
        for k, v in columns.items()
    ]
    placeholders = ", ".join(["%s"] * len(names))
    return (
        f"INSERT INTO listing_submissions ({', '.join(names)}) VALUES ({placeholders})",  # noqa: S608
        values,
    )


def _attempt(
    db: Conn, fl: Flow, job: UUID, previous: UUID, **columns: Any
) -> tuple[str, list[Any]]:
    """The INSERT of the attempt after `previous`, as the submission() factory would write it."""
    row = db.execute(
        "SELECT attempt FROM listing_submissions WHERE id = %s", (previous,)
    ).fetchone()
    assert row is not None
    payload = bytes([row[0] + 1] * 32)
    values: dict[str, Any] = {
        "tenant_id": fl.tenant_id,
        "listing_id": fl.listing_id,
        "listing_version_id": fl.v1,
        "marketplace": "amazon",
        "marketplace_account_id": fl.account_id,
        "job_id": job,
        "attempt": row[0] + 1,
        "operation": "create",
        "payload_sha256": payload,
        "payload_storage_key": f"{fl.tenant_id}/submissions/{payload.hex()}.json",
        "facts_digest": bytes(range(32)),
        "idempotency_key": payload.hex(),
        "requested_by_user_id": fl.operator,
        "actor_type": "user",
        "retry_justification": "retryable_condition",
        "previous_submission_id": previous,
        **columns,
    }
    return _insert_sql(**values)


def test_ls_guard_attempt1_passes(db: Conn) -> None:
    fl, job = _publishing(db)
    submission(db, fl, job)


def test_ls_guard_not_publishing(db: Conn) -> None:
    fl, job = _publishing(db)
    force_state(db, fl.listing_id, "approved")
    with pytest.raises(psycopg.Error) as raised, db.transaction():
        submission(db, fl, job)
    assert raised.value.sqlstate == "GRD01"


def test_ls_guard_not_current_approval(db: Conn) -> None:
    fl, job = _publishing(db)
    v2 = next_version(db, fl.tenant_id, fl.listing_id, fl.v1, "regeneration", fl.operator)
    for version in (fl.v1, v2):
        with pytest.raises(psycopg.Error) as raised, db.transaction():
            submission(db, fl, job, listing_version_id=version)
        assert raised.value.sqlstate == "GRD01"


def test_ls_guard_facts_digest_mismatch(db: Conn) -> None:
    fl, job = _publishing(db)
    with pytest.raises(psycopg.Error) as raised, db.transaction():
        submission(db, fl, job, facts_digest=bytes(32))
    assert raised.value.sqlstate == "SUB04"


def test_ls_guard_paused(db: Conn) -> None:
    """A publishing listing cannot be paused (ck_listings_pause_archive), so the guard's paused
    clause is the second wall; the tenant clause is reachable."""
    fl, _ = _publishing(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET paused_at = now() WHERE id = %s",
        (fl.listing_id,),
        constraint="ck_listings_pause_archive",
    )


def test_ls_guard_suspended_tenant(db: Conn) -> None:
    fl, job = _publishing(db)
    db.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (fl.tenant_id,))
    with pytest.raises(psycopg.Error) as raised, db.transaction():
        submission(db, fl, job)
    assert raised.value.sqlstate == "GRD01"


def _chain(db: Conn, fl: Flow, job: UUID, n: int) -> UUID:
    """n failed (retryable error) attempts; returns the last one."""
    previous = submission(db, fl, job)
    _fail(db, previous, retryable=True)
    for _ in range(n - 1):
        query, params = _attempt(db, fl, job, previous)
        db.execute(query, params)
        previous = db.execute(
            "SELECT id FROM listing_submissions WHERE listing_id = %s ORDER BY attempt DESC LIMIT 1",
            (fl.listing_id,),
        ).fetchone()[
            0
        ]  # type: ignore[index]
        _fail(db, previous, retryable=True)
    return previous


def test_ls_guard_fourth_attempt(db: Conn) -> None:
    fl, job = _publishing(db)
    last = _chain(db, fl, job, 3)
    query, params = _attempt(db, fl, job, last, attempt=4)
    expect_sqlstate(db, "SUB02", query, params)


def test_ls_guard_attempt_gap(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    query, params = _attempt(db, fl, job, first, attempt=3)
    expect_sqlstate(db, "GRD01", query, params)


def test_ls_guard_previous_must_be_resolved(db: Conn) -> None:
    fl, job = _publishing(db)
    first = submission(db, fl, job)
    db.execute(
        "UPDATE listing_submissions SET outcome = 'accepted', responded_at = now() WHERE id = %s",
        (first,),
    )
    query, params = _attempt(db, fl, job, first)
    # the open accepted row: refused by the guard before the open-row unique
    expect_sqlstate(db, "GRD01", query, params)


def _failed_field(db: Conn, fl: Flow, sub: UUID, field: str) -> None:
    item = f.listing_item(
        db, fl.tenant_id, fl.listing_id, fl.product_id, seller_sku=_sku(db, fl.product_id)
    )
    f.insert(
        db,
        "publish_field_results",
        tenant_id=fl.tenant_id,
        submission_id=sub,
        listing_id=fl.listing_id,
        listing_version_id=fl.v1,
        listing_item_id=item,
        field=field,
        status="failed",
        error_json={"error_code": "VALUE_NOT_ALLOWED"},
    )


def _sku(db: Conn, product: UUID) -> str:
    row = db.execute("SELECT sku FROM products WHERE id = %s", (product,)).fetchone()
    assert row is not None
    return str(row[0])


CHANGE = {
    "field": "content.title",
    "listing_item_id": None,
    "from_digest": "a",
    "to_digest": "b",
    "source": "human_edit",
}


def test_ls_guard_changed_input_same_hash(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    same = scalar(db, "SELECT payload_sha256 FROM listing_submissions WHERE id = %s", (first,))
    query, params = _attempt(
        db,
        fl,
        job,
        first,
        retry_justification="changed_input",
        payload_sha256=same,
        corrections_json=[CHANGE],
    )
    expect_sqlstate(db, "SUB01", query, params)


def test_ls_guard_changed_input_resends_failed_values(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    _failed_field(db, fl, first, "content.title")
    unchanged = {**CHANGE, "to_digest": "a"}
    query, params = _attempt(
        db,
        fl,
        job,
        first,
        retry_justification="changed_input",
        operation="patch",
        field_paths=["content.title"],
        corrections_json=[unchanged],
    )
    expect_sqlstate(db, "SUB01", query, params)
    other_field = {**CHANGE, "field": "content.bullets"}
    query, params = _attempt(
        db,
        fl,
        job,
        first,
        retry_justification="changed_input",
        operation="patch",
        field_paths=["content.title"],
        corrections_json=[other_field],
    )
    expect_sqlstate(db, "SUB01", query, params)


def test_ls_guard_changed_input_ok(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    _failed_field(db, fl, first, "content.title")
    query, params = _attempt(
        db,
        fl,
        job,
        first,
        retry_justification="changed_input",
        operation="patch",
        field_paths=["content.title"],
        corrections_json=[CHANGE],
    )
    db.execute(query, params)


def test_ls_guard_retryable_condition_ok(db: Conn) -> None:
    fl, job = _publishing(db)
    first = submission(db, fl, job)
    _fail(db, first, retryable=False)
    query, params = _attempt(db, fl, job, first)
    expect_sqlstate(db, "SUB01", query, params)
    db.execute("SET LOCAL session_replication_role = replica")  # rewrite the report (set-up)
    db.execute(
        "UPDATE listing_submissions SET error_report_json = %s WHERE id = %s",
        (Jsonb({"status": "failed", "retryable": True, "errors": []}), first),
    )
    db.execute("SET LOCAL session_replication_role = origin")
    db.execute(query, params)


def test_ls_guard_marketplace_transition_ok(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    query, params = _attempt(db, fl, job, first, retry_justification="marketplace_transition")
    expect_sqlstate(db, "SUB01", query, params)
    db.execute(
        "UPDATE listings SET marketplace_state_changed_at = now() + interval '1 second' "
        "WHERE id = %s",
        (fl.listing_id,),
    )
    db.execute(query, params)


def test_ls_guard_human_resolution_ok(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    query, params = _attempt(db, fl, job, first, retry_justification="human_resolution")
    expect_sqlstate(db, "SUB01", query, params)
    task = f.human_task(
        db,
        fl.tenant_id,
        kind="reconnect_account",
        product_id=None,
        target_path="account.connection",
        marketplace_account_id=fl.account_id,
        expected_answer={"kind": "reconnect"},
        resume_from="publish",
        status="applied",
        answer={"value": True},
        answered_by_user_id=fl.owner,
        answered_at="2099-01-01T00:00:00Z",
        applied_at="2099-01-01T00:00:00Z",
    )
    f.listing_issue(
        db,
        fl.tenant_id,
        fl.listing_id,
        job_id=job,
        human_task_id=task,
        source="amazon_adapter",
        error_code="ACCOUNT_PERMISSION_DENIED",
        error_class="account_permission",
        retry_class="user_action",
        status="resolved",
        resolution="human_answer",
        resolved_at="2099-01-01T00:00:00Z",
    )
    db.execute(query, params)


def test_ls_guard_system_needs_same_requester(db: Conn) -> None:
    fl, job = _publishing(db)
    first = _chain(db, fl, job, 1)
    _failed_field(db, fl, first, "content.title")
    columns = {
        "retry_justification": "changed_input",
        "actor_type": "system",
        "operation": "patch",
        "field_paths": ["content.title"],
        "corrections_json": [CHANGE],
    }
    query, params = _attempt(db, fl, job, first, requested_by_user_id=fl.owner, **columns)
    expect_sqlstate(db, "SUB01", query, params)
    query, params = _attempt(db, fl, job, first, **columns)
    db.execute(query, params)


# ---- BEFORE UPDATE: the outcome ledger ----


def test_ls_guard_outcome_edges(db: Conn) -> None:
    fl, job = _publishing(db)
    sub = submission(db, fl, job)
    expect_sqlstate(
        db,
        "SUB03",
        "UPDATE listing_submissions SET outcome = 'superseded', resolved_at = now() "
        "WHERE id = %s",
        (sub,),
    )
    expect_sqlstate(db, "SUB03", "UPDATE listing_submissions SET attempt = 2 WHERE id = %s", (sub,))
    db.execute(
        "UPDATE listing_submissions SET outcome = 'accepted', responded_at = now() WHERE id = %s",
        (sub,),
    )
    expect_sqlstate(
        db,
        "SUB03",
        "UPDATE listing_submissions SET outcome = 'error', error_code = 'RATE_LIMITED', "
        "error_report_json = '{}', resolved_at = now() WHERE id = %s",
        (sub,),
    )
    db.execute(
        "UPDATE listing_submissions SET outcome = 'error', error_code = 'MARKETPLACE_UNCONFIRMED', "
        "error_report_json = '{}', resolved_at = now() WHERE id = %s",
        (sub,),
    )
    expect_sqlstate(
        db, "SUB03", "UPDATE listing_submissions SET submission_ref = 'x' WHERE id = %s", (sub,)
    )


def test_ls_guard_error_after_send_needs_resolving_code(db: Conn) -> None:
    fl, job = _publishing(db)
    sub = submission(db, fl, job, sent_at="2026-10-06T00:00:00Z")
    expect_sqlstate(
        db,
        "SUB03",
        "UPDATE listing_submissions SET outcome = 'error', error_code = 'TIMEOUT', "
        "error_report_json = '{}', responded_at = now(), resolved_at = now() WHERE id = %s",
        (sub,),
    )
    db.execute(
        "UPDATE listing_submissions SET outcome = 'error', error_code = 'SUBMISSION_NOT_APPLIED', "
        "error_report_json = '{}', responded_at = now(), resolved_at = now() WHERE id = %s",
        (sub,),
    )


# ---- two connections: two publish jobs, one listing (ux_ls_open, DATABASE.md:1792) ----


@pytest.fixture(scope="module")
def race_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("ls_race")) as scratch:
        yield scratch


def test_ls_guard_race_two_jobs_one_wins(race_db: ScratchDb) -> None:
    with psycopg.connect(race_db.libpq) as setup:
        fl, job = _publishing(setup)
        setup.commit()
    outcomes: list[str | None] = []
    barrier = threading.Barrier(2)

    def send(n: int) -> None:
        with psycopg.connect(race_db.libpq) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(fl.tenant_id)])
            conn.execute("SET ROLE app_rw")
            try:
                barrier.wait(timeout=5)
                submission(conn, fl, job, idempotency_key=f"race-{n}")
                conn.commit()
                outcomes.append(None)
            except psycopg.Error as error:
                conn.rollback()
                outcomes.append(error.sqlstate)

    threads = [threading.Thread(target=send, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(outcomes, key=str) in (
        sorted([None, "23505"], key=str),  # ux_ls_open
        sorted([None, "GRD01"], key=str),  # the loser saw attempt 1 taken (attempt gap)
    )
    with psycopg.connect(race_db.libpq) as check:
        count = check.execute(
            "SELECT count(*) FROM listing_submissions WHERE listing_id = %s", (fl.listing_id,)
        ).fetchone()
    assert count == (1,)
