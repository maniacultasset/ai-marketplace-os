"""Test support for the 0004 guards: role switches, the GUC setter, and row factories over the
`ops_support.seed_graph` two-tenant graph. Each factory inserts on the caller's connection and
transaction and returns the new key; keyword arguments override or add columns.

`as_migrator` changes the *session* user, because the append-only triggers check
`session_user = 'migrator'` (DATABASE.md §5.2 audit_log "Append-only"); `as_app_rw` changes only
`current_user`, which is what privilege checks use."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

Conn = psycopg.Connection[tuple[Any, ...]]


@contextmanager
def as_migrator(conn: Conn) -> Iterator[Conn]:
    """`SET SESSION AUTHORIZATION migrator` for the block (the caller logs in as a superuser)."""
    conn.execute("SET SESSION AUTHORIZATION migrator")
    try:
        yield conn
    finally:
        conn.execute("RESET SESSION AUTHORIZATION")


@contextmanager
def as_app_rw(conn: Conn) -> Iterator[Conn]:
    """`SET ROLE app_rw` for the block: privileges are checked as `app_rw`."""
    conn.execute("SET ROLE app_rw")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


def set_gucs(conn: Conn, **values: object) -> None:
    """`set_config('app.<name>', value, true)` for each keyword (transaction-local); None clears
    the setting to '' (what a pooled connection holds after a reset)."""
    for name, value in values.items():
        conn.execute(
            "SELECT set_config(%s, %s, true)",
            (f"app.{name}", "" if value is None else str(value)),
        )


def scalar(conn: Conn, query: str, params: Any = None) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def uuid_of(value: Any) -> UUID:
    assert isinstance(value, UUID), value
    return value


# ---- versions (lv_guard) ----


def make_current(conn: Conn, listing_id: UUID, version_id: UUID) -> None:
    conn.execute(
        "UPDATE listings SET current_version_id = %s WHERE id = %s", (version_id, listing_id)
    )


def next_version(
    conn: Conn,
    tenant_id: UUID,
    listing_id: UUID,
    parent: UUID | None,
    origin: str,
    generated_by: UUID,
    *,
    edited_by: UUID | None = None,
    changed_paths: list[str] | None = None,
    current: bool = True,
) -> UUID:
    """A version of `listing_id` under `parent`, made current unless `current=False`."""
    from . import factories as f

    if origin == "edit" and changed_paths is None:
        changed_paths = ["content.title"]
    version_id = f.version(
        conn,
        tenant_id,
        listing_id,
        origin=origin,
        parent_version_id=parent,
        generated_by_user_id=generated_by,
        edited_by_user_id=edited_by,
        changed_paths=changed_paths,
    )
    if current:
        make_current(conn, listing_id, version_id)
    return uuid_of(version_id)


def approve(conn: Conn, version_id: UUID, approver: UUID) -> None:
    conn.execute(
        "UPDATE listing_versions SET approved_by_user_id = %s WHERE id = %s", (approver, version_id)
    )


APPROVE_SQL = "UPDATE listing_versions SET approved_by_user_id = %s WHERE id = %s"


# ---- listings (status guard): a listing in any state, then a guarded transition ----

DEFAULT_REASON: dict[str, str] = {"blocked": "validation_errors", "failed": "publish_failed"}


@dataclass(frozen=True)
class Flow:
    """One tenant's listing with an active Amazon account and version 1 current (by `operator`)."""

    tenant_id: UUID
    product_id: UUID
    account_id: UUID
    listing_id: UUID
    v1: UUID
    owner: UUID
    operator: UUID
    reviewer: UUID


def flow(conn: Conn, **tenant_columns: Any) -> Flow:
    from . import factories as f

    tenant = f.tenant(conn, **tenant_columns)
    owner = f.member(conn, tenant, role="owner")
    operator = f.member(conn, tenant, role="operator")
    reviewer = f.member(conn, tenant, role="reviewer")
    product = f.product(conn, tenant)
    account = f.marketplace_account(conn, tenant, connected_by_user_id=owner)
    listing = f.listing(conn, tenant, product, marketplace_account_id=account)
    v1 = next_version(conn, tenant, listing, None, "generation", operator)
    return Flow(tenant, product, account, listing, v1, owner, operator, reviewer)


def force_state(
    conn: Conn, listing_id: UUID, status: str, reason: str | None = None, **cols: Any
) -> None:
    """Put a listing in `status` without the guards (test set-up only: a superuser skips user
    triggers for this one statement; CHECKs and the row itself are unchanged otherwise)."""
    values: dict[str, Any] = {
        "status": status,
        "status_reason": reason if reason is not None else DEFAULT_REASON.get(status),
        **cols,
    }
    names = list(values)
    query = sql.SQL("UPDATE listings SET {} WHERE id = %s").format(
        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(n)) for n in names)
    )
    conn.execute("SET LOCAL session_replication_role = replica")
    try:
        conn.execute(query, [*values.values(), listing_id])
    finally:
        conn.execute("SET LOCAL session_replication_role = origin")


def approve_current(conn: Conn, fl: Flow, version_id: UUID | None = None) -> UUID:
    """Approve `version_id` (default v1) by the reviewer and point approved_version_id at it, through
    the guards (the version must be current). Returns the version id."""
    version = version_id or fl.v1
    approve(conn, version, fl.reviewer)
    conn.execute(
        "UPDATE listings SET approved_version_id = %s WHERE id = %s", (version, fl.listing_id)
    )
    return version


def transition(
    conn: Conn, listing_id: UUID, to: str, event: str, reason: str | None = None, **cols: Any
) -> None:
    values: dict[str, Any] = {"status": to, "status_reason": reason, "last_event": event, **cols}
    names = list(values)
    query = sql.SQL("UPDATE listings SET {} WHERE id = %s").format(
        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(n)) for n in names)
    )
    conn.execute(query, [*values.values(), listing_id])


def transition_sql(
    listing_id: UUID, to: str, event: str, reason: str | None = None, **cols: Any
) -> tuple[sql.Composed, list[Any]]:
    values: dict[str, Any] = {"status": to, "status_reason": reason, "last_event": event, **cols}
    query = sql.SQL("UPDATE listings SET {} WHERE id = %s").format(
        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(n)) for n in values)
    )
    return query, [*values.values(), listing_id]


def queued_job(conn: Conn, fl: Flow, kind: str, **columns: Any) -> UUID:
    """A queued listing job of `kind` on the flow's listing, created by its operator."""
    from . import factories as f

    defaults: dict[str, Any] = {
        "tenant_id": fl.tenant_id,
        "kind": kind,
        "listing_id": fl.listing_id,
        "entity_type": "listing",
        "entity_id": fl.listing_id,
        "payload_json": {"listing_id": str(fl.listing_id)},
        "correlation_id": uuid4(),
        "created_by": fl.operator,
    }
    return uuid_of(f.insert(conn, "job_runs", **{**defaults, **columns}))


def decision(
    conn: Conn, fl: Flow, kind: str, version_id: UUID | None, by: UUID, **columns: Any
) -> UUID:
    """A review decision decided now by `by` (the guard's "in this transaction" test)."""
    from . import factories as f

    if kind == "reject":
        columns.setdefault("reason_code", "poor_quality")
        columns.setdefault("feedback", "Needs work.")
    return uuid_of(
        f.review_decision(
            conn,
            fl.tenant_id,
            fl.listing_id,
            version_id,
            decision=kind,
            decided_by_user_id=by,
            **columns,
        )
    )


PUBLISH_EVENTS = frozenset({"publish", "retry_publish", "auto_resubmit", "resume_publish"})
DIGEST = bytes(range(32))  # the facts digest the approval relied on


def submission(conn: Conn, fl: Flow, job_id: UUID, **columns: Any) -> UUID:
    """A `pending` attempt-1 `create` of v1 by the operator, with the approval's facts digest."""
    from . import factories as f

    n = uuid4().hex
    defaults: dict[str, Any] = {
        "tenant_id": fl.tenant_id,
        "listing_id": fl.listing_id,
        "listing_version_id": fl.v1,
        "marketplace": "amazon",
        "marketplace_account_id": fl.account_id,
        "job_id": job_id,
        "attempt": 1,
        "operation": "create",
        "payload_sha256": bytes.fromhex(n + n),
        "payload_storage_key": f"{fl.tenant_id}/submissions/{n}.json",
        "facts_digest": DIGEST,
        "idempotency_key": n,
        "requested_by_user_id": fl.operator,
        "actor_type": "user",
    }
    return uuid_of(f.insert(conn, "listing_submissions", **{**defaults, **columns}))


def approved_flow(conn: Conn, fl: Flow) -> UUID:
    """v1 approved by the reviewer and current, with its `approve` decision (facts digest)."""
    decision(conn, fl, "approve", fl.v1, fl.reviewer, facts_digest=DIGEST)
    return approve_current(conn, fl)


def prepare_edge(
    conn: Conn,
    edge: tuple[str, str, str, str, bool, bool, list[str] | None, list[str] | None],
) -> tuple[Flow, sql.Composed, list[Any]]:
    """A listing in the edge's `from` state with everything its event needs, and the guarded
    UPDATE that takes the edge (DATABASE.md §6.1 event rules, §5.3 statement order)."""
    src, to, event, _, needs_approval, voids, allowed, from_reason = edge
    fl = flow(conn)
    set_gucs(conn, user_id=fl.reviewer, job_id=uuid4())
    cols: dict[str, Any] = {}
    if src in ("approved", "publishing", "published") or needs_approval:
        approved_flow(conn, fl)
    if src == "publishing":
        force_state(conn, fl.listing_id, "approved")
        publish_job = queued_job(conn, fl, "listing_publish")
        conn.execute(
            "UPDATE job_runs SET status = 'running', started_at = now(), "
            "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
            (publish_job,),
        )
    if src == "published":
        cols.update(
            published_version_id=fl.v1,
            first_published_at="2026-10-01T00:00:00Z",
            marketplace_state="processing",
        )
    force_state(conn, fl.listing_id, src, from_reason[0] if from_reason else None, **cols)

    update: dict[str, Any] = {}
    if voids:
        update["approved_version_id"] = None
    if event == "edit":
        update["current_version_id"] = next_version(
            conn,
            fl.tenant_id,
            fl.listing_id,
            fl.v1,
            "edit",
            fl.operator,
            edited_by=fl.operator,
            current=False,
        )
    if event == "approve":
        approved_flow(conn, fl)
    if event in ("reject", "reject_regenerate"):
        decision(conn, fl, "reject", fl.v1, fl.reviewer)
    if event in ("generate", "regenerate", "reject_regenerate"):
        job = queued_job(conn, fl, "listing_generate")
        if event == "regenerate":
            decision(conn, fl, "regenerate_requested", fl.v1, fl.reviewer, regeneration_job_id=job)
    if event in PUBLISH_EVENTS:
        queued_job(conn, fl, "listing_publish")
    if event == "submission_accepted":
        sub = submission(conn, fl, publish_job)
        conn.execute(
            "UPDATE listing_submissions SET outcome = 'accepted', responded_at = now() WHERE id = %s",
            (sub,),
        )
        update.update(
            published_version_id=fl.v1,
            first_published_at="2026-10-06T00:00:00Z",
            marketplace_state="processing",
        )
    query, params = transition_sql(
        fl.listing_id, to, event, allowed[0] if allowed else None, **update
    )
    return fl, query, params


def disable_0004_guard(conn: Conn, table: str, trigger: str) -> None:
    """For the earlier steps' CHECK/FK/key suites only: switch one 0004 guard trigger off inside
    the caller's transaction (which the `db` fixture rolls back, so the catalog is unchanged
    afterwards). The guard itself is proven by its own test_0004_* suite."""
    conn.execute(
        sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
            sql.Identifier(table), sql.Identifier(trigger)
        )
    )
