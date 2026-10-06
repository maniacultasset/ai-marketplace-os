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
