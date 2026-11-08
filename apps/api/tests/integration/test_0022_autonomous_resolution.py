"""Revision 0022_autonomous_resolution (SI11 T1-T2): the round trip, the `auto_fix` and
`corrections_exhausted` edges with their event rule, the `listings.remote_check_*` columns, and the
downgrade that refuses while an `auto_fix` row exists."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import (
    approved_flow,
    as_app_rw,
    as_migrator,
    flow,
    force_state,
    next_version,
    prepare_edge,
    queued_job,
    scalar,
    set_gucs,
    transition_sql,
)
from .test_0004_transitions import SI11_EDGES

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0021_ai_credit_state"
REVISION = "0022_autonomous_resolution"
EDGE_SQL = (
    "SELECT from_status, to_status, event, actor, requires_current_approval, voids_approval, "
    "allowed_reasons, requires_from_reason FROM listing_status_transitions "
    "WHERE from_status = %s AND to_status = %s AND event = %s"
)


def _has_column(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return bool(
            scalar(
                conn,
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'listings' AND column_name = 'remote_check_status'",
            )
        )


def _edge_count(libpq: str) -> int:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return int(scalar(conn, "SELECT count(*) FROM listing_status_transitions"))


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not _has_column(scratch_db.libpq)
    assert _edge_count(scratch_db.libpq) == 63
    command.upgrade(cfg, REVISION)
    assert _has_column(scratch_db.libpq)
    assert _edge_count(scratch_db.libpq) == 72
    command.upgrade(cfg, "head")  # 0023 (SI12) sits on top: `check` compares at the head
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_column(scratch_db.libpq)
    assert _edge_count(scratch_db.libpq) == 63
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        body = str(
            scalar(conn, "SELECT prosrc FROM pg_proc WHERE proname = 'listings_status_events'")
        )
        assert "auto_fix" not in body
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_refused_with_auto_fix_row(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        # the scratch database has no synced dictionary: skip the guards for the set-up rows
        conn.execute("SET session_replication_role = replica")
        fl = flow(conn)
        conn.execute(
            "UPDATE listings SET status = 'generating', last_event = 'auto_fix' WHERE id = %s",
            (fl.listing_id,),
        )
        conn.commit()
    with pytest.raises(Exception, match="downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    assert _has_column(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


# ---- the 9 edges ----


@pytest.mark.parametrize("key", [e[:3] for e in SI11_EDGES], ids=lambda k: "-".join(k))
def test_si11_edges_pass(db: Conn, key: tuple[str, str, str]) -> None:
    edge = db.execute(EDGE_SQL, key).fetchone()
    assert edge is not None
    _, query, params = prepare_edge(db, edge)
    db.execute(query, params)
    row = db.execute("SELECT status, last_event FROM listings WHERE id = %s", (params[-1],))
    assert row.fetchone() == (key[1], key[2])


def _in_state(db: Conn, status: str, reason: str | None = None) -> Any:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    force_state(db, fl.listing_id, status, reason)
    return fl


def test_auto_fix_needs_queued_generate_job(db: Conn) -> None:
    fl = _in_state(db, "validated")
    query, params = transition_sql(fl.listing_id, "generating", "auto_fix")
    error = expect_sqlstate(db, "LST01", query, params)
    assert "listing_generate" in str(error)
    queued_job(db, fl, "listing_generate")
    db.execute(query, params)
    assert scalar(db, "SELECT status FROM listings WHERE id = %s", (fl.listing_id,)) == (
        "generating"
    )


def test_auto_fix_needs_a_job_actor(db: Conn) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer)  # a user, no job
    force_state(db, fl.listing_id, "validated")
    queued_job(db, fl, "listing_generate")
    query, params = transition_sql(fl.listing_id, "generating", "auto_fix")
    expect_sqlstate(db, "LST01", query, params)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("blocked", "human_task_open"),
        ("failed", "generation_failed"),
        ("created", None),
        ("publishing", None),
    ],
)
def test_auto_fix_refused_elsewhere(db: Conn, status: str, reason: str | None) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    if status == "publishing":
        approved_flow(db, fl)
    force_state(db, fl.listing_id, status, reason)
    queued_job(db, fl, "listing_generate")
    query, params = transition_sql(fl.listing_id, "generating", "auto_fix")
    expect_sqlstate(db, "LST01", query, params)


def test_corrections_exhausted_refused_from_approved(db: Conn) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    approved_flow(db, fl)
    force_state(db, fl.listing_id, "approved")
    query, params = transition_sql(
        fl.listing_id,
        "blocked",
        "corrections_exhausted",
        "corrections_exhausted",
        approved_version_id=None,
    )
    expect_sqlstate(db, "LST01", query, params)


@pytest.mark.parametrize("status", ["approved", "published"])
def test_auto_fix_voids_approval_keeps_live(db: Conn, status: str) -> None:
    fl = flow(db)
    set_gucs(db, user_id=fl.reviewer, job_id=uuid4())
    approved_flow(db, fl)
    cols: dict[str, Any] = {}
    if status == "published":
        cols = {
            "published_version_id": fl.v1,
            "live_version_id": fl.v1,
            "first_published_at": "2026-10-01T00:00:00Z",
            "marketplace_state": "live",
            "live_confirmed_at": "2026-10-01T00:00:00Z",
            "last_synced_at": "2026-10-01T00:00:00Z",
        }
    force_state(db, fl.listing_id, status, **cols)
    queued_job(db, fl, "listing_generate")
    keeping = transition_sql(fl.listing_id, "generating", "auto_fix")
    expect_sqlstate(db, "LST01", *keeping)  # the approval must be voided
    db.execute(*transition_sql(fl.listing_id, "generating", "auto_fix", approved_version_id=None))
    row = db.execute(
        "SELECT approved_version_id, live_version_id FROM listings WHERE id = %s",
        (fl.listing_id,),
    ).fetchone()
    assert row == (None, fl.v1 if status == "published" else None)


# ---- listings.remote_check_* ----


def test_remote_check_all_null_ok(db: Conn) -> None:
    fl = flow(db)
    row = db.execute(
        "SELECT remote_check_version_id, remote_check_status, remote_check_at FROM listings "
        "WHERE id = %s",
        (fl.listing_id,),
    ).fetchone()
    assert row == (None, None, None)


def test_remote_check_pending_without_at_ok(db: Conn) -> None:
    fl = flow(db)
    db.execute(
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = 'pending' "
        "WHERE id = %s",
        (fl.v1, fl.listing_id),
    )
    db.execute(
        "UPDATE listings SET remote_check_status = 'clean', remote_check_at = now() WHERE id = %s",
        (fl.listing_id,),
    )


@pytest.mark.parametrize(
    ("status", "with_version", "with_at"),
    [
        ("clean", True, False),
        ("issues", True, False),
        ("pending", False, False),
        ("done", True, True),
    ],
)
def test_remote_check_refused(db: Conn, status: str, with_version: bool, with_at: bool) -> None:
    fl = flow(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = %s, "
        "remote_check_at = %s WHERE id = %s",
        (
            fl.v1 if with_version else None,
            status,
            "2026-11-08T00:00:00Z" if with_at else None,
            fl.listing_id,
        ),
        constraint="ck_listings_remote_check",
    )


def test_remote_check_version_without_status_refused(db: Conn) -> None:
    fl = flow(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET remote_check_version_id = %s WHERE id = %s",
        (fl.v1, fl.listing_id),
        constraint="ck_listings_remote_check",
    )


def test_remote_check_other_listings_version_refused(db: Conn) -> None:
    fl = flow(db)
    other = f.listing(db, fl.tenant_id, f.product(db, fl.tenant_id))
    v_other = next_version(db, fl.tenant_id, other, None, "generation", fl.operator)
    expect_sqlstate(
        db,
        "23503",
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = 'pending' "
        "WHERE id = %s",
        (v_other, fl.listing_id),
        constraint="fk_listings_remote_check_version_id",
    )


def test_remote_check_fk_is_deferrable(db: Conn) -> None:
    row = db.execute(
        "SELECT condeferrable, condeferred, convalidated FROM pg_constraint "
        "WHERE conname = 'fk_listings_remote_check_version_id'"
    ).fetchone()
    assert row == (True, False, True)


# ---- listing_repairs (T2) ----


def _repair_world(db: Conn) -> Any:
    fl = flow(db)
    item = f.listing_item(db, fl.tenant_id, fl.listing_id, fl.product_id)
    return fl, item


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"rule": "nearest"}, "ck_listing_repairs_rule"),
        ({"source_digest": bytes(31)}, "ck_listing_repairs_digests"),
        ({"to_digest": bytes(33)}, "ck_listing_repairs_digests"),
        ({"value_json": None}, "ck_listing_repairs_value_json"),
        ({"rule": "drop_locked"}, "ck_listing_repairs_value_json"),
        ({"value_json": [{"value": "x" * 2049}]}, "ck_listing_repairs_value_json"),
        ({"retired_reason": "superseded"}, "ck_listing_repairs_retired"),
        ({"retired_at": "2026-11-08T00:00:00Z"}, "ck_listing_repairs_retired"),
        (
            {"retired_at": "2026-11-08T00:00:00Z", "retired_reason": "bored"},
            "ck_listing_repairs_retired",
        ),
        ({"attribute": "Brand"}, "ck_listing_repairs_attribute"),
        ({"attribute": ""}, "ck_listing_repairs_attribute"),
        ({"rule": "unit_format", "alias_version": 1}, "ck_listing_repairs_alias_version"),
        ({"alias_version": 0}, "ck_listing_repairs_alias_version"),
        ({"field_path": ""}, "ck_listing_repairs_field_path"),
        ({"field_path": "x" * 193}, "ck_listing_repairs_field_path"),
        ({"issue_key": "k" * 401}, "ck_listing_repairs_issue_key"),
    ],
)
def test_repair_checks_refuse(db: Conn, columns: dict[str, Any], constraint: str) -> None:
    fl = flow(db)
    with pytest.raises(psycopg.errors.CheckViolation) as caught, db.transaction():
        f.listing_repair(db, fl.tenant_id, fl.listing_id, **columns)
    assert caught.value.diag.constraint_name == constraint


def test_repair_valid_shapes(db: Conn) -> None:
    fl, item = _repair_world(db)
    f.listing_repair(db, fl.tenant_id, fl.listing_id, alias_version=1)
    f.listing_repair(
        db, fl.tenant_id, fl.listing_id, listing_item_id=item, attribute="item_type_keyword",
        rule="drop_locked", value_json=None,
    )  # fmt: skip


def test_repair_one_live_per_attribute(db: Conn) -> None:
    fl, item = _repair_world(db)
    first = f.listing_repair(db, fl.tenant_id, fl.listing_id)
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        f.listing_repair(db, fl.tenant_id, fl.listing_id)  # NULL item on both: NOT DISTINCT
    assert caught.value.diag.constraint_name == "uq_listing_repairs_live"
    f.listing_repair(db, fl.tenant_id, fl.listing_id, listing_item_id=item)  # another item
    db.execute(
        "UPDATE listing_repairs SET retired_at = now(), retired_reason = 'superseded' "
        "WHERE id = %s",
        (first,),
    )
    f.listing_repair(db, fl.tenant_id, fl.listing_id)  # a retired row frees the slot


def test_repair_one_try_per_key_even_after_retire(db: Conn) -> None:
    fl = flow(db)
    first = f.listing_repair(db, fl.tenant_id, fl.listing_id, issue_key="k1")
    db.execute(
        "UPDATE listing_repairs SET retired_at = now(), retired_reason = 'source_changed' "
        "WHERE id = %s",
        (first,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation) as caught, db.transaction():
        f.listing_repair(db, fl.tenant_id, fl.listing_id, issue_key="k1")
    assert caught.value.diag.constraint_name == "uq_listing_repairs_once"
    f.listing_repair(db, fl.tenant_id, fl.listing_id, issue_key="k1", rule="unit_format")


def test_repair_retires_once(db: Conn) -> None:
    fl = flow(db)
    repair = f.listing_repair(db, fl.tenant_id, fl.listing_id)
    retire = "UPDATE listing_repairs SET retired_at = now(), retired_reason = %s WHERE id = %s"
    with as_app_rw(db, fl.tenant_id):
        db.execute(retire, ("superseded", repair))
        expect_sqlstate(db, "GRD01", retire, ("source_changed", repair))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE listing_repairs SET value_json = '[]'::jsonb WHERE id = %s",
        "UPDATE listing_repairs SET source_digest = '\\x00'::bytea WHERE id = %s",
        "DELETE FROM listing_repairs WHERE id = %s",
    ],
)
def test_repair_app_rw_privileges(db: Conn, statement: str) -> None:
    fl = flow(db)
    repair = f.listing_repair(db, fl.tenant_id, fl.listing_id)
    with as_app_rw(db, fl.tenant_id):
        expect_sqlstate(db, "42501", statement, (repair,))


def test_repair_rls_other_tenant(db: Conn) -> None:
    a, b = flow(db), flow(db)
    f.listing_repair(db, a.tenant_id, a.listing_id)
    with as_app_rw(db, b.tenant_id):
        assert scalar(db, "SELECT count(*) FROM listing_repairs") == 0
        # B's tenant id on A's listing: the composite FK refuses it (A's listing is not B's)
        expect_sqlstate(
            db,
            "23503",
            "INSERT INTO listing_repairs (tenant_id, listing_id, marketplace, field_path, "
            "attribute, rule, issue_key, source_digest, to_digest, value_json) VALUES "
            "(%s, %s, 'amazon', 'facts.attributes.size', 'size_name', 'enum_fold', 'k', %s, %s, "
            "'[]'::jsonb)",
            (b.tenant_id, a.listing_id, bytes(32), bytes(32)),
        )
        # A's tenant id under B's GUC: RLS refuses it
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO listing_repairs (tenant_id, listing_id, marketplace, field_path, "
            "attribute, rule, issue_key, source_digest, to_digest, value_json) VALUES "
            "(%s, %s, 'amazon', 'facts.attributes.size', 'size_name', 'enum_fold', 'k2', %s, %s, "
            "'[]'::jsonb)",
            (a.tenant_id, a.listing_id, bytes(32), bytes(32)),
        )


def test_repair_read_roles_never_see_value_json(db: Conn) -> None:
    fl = flow(db)
    f.listing_repair(db, fl.tenant_id, fl.listing_id)
    for role in ("app_ro", "app_platform_ro"):
        with db.transaction():
            db.execute(f"SET LOCAL ROLE {role}")
            assert scalar(db, "SELECT count(*) FROM listing_repairs") >= 1
            assert scalar(db, "SELECT rule FROM listing_repairs LIMIT 1") == "enum_fold"
            expect_sqlstate(db, "42501", "SELECT value_json FROM listing_repairs")


def test_repair_cascades_with_account_purge(db: Conn) -> None:
    fl, item = _repair_world(db)
    f.listing_repair(db, fl.tenant_id, fl.listing_id, listing_item_id=item)
    db.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, revoked_at = now() - interval '30 days' WHERE id = %s",
        (fl.account_id,),
    )
    with as_migrator(db):
        db.execute("SELECT purge_marketplace_account(%s, %s)", (fl.tenant_id, fl.account_id))
    assert (
        scalar(db, "SELECT count(*) FROM listing_repairs WHERE tenant_id = %s", (fl.tenant_id,))
        == 0
    )


def test_repair_audit_allowlist(db: Conn) -> None:
    assert scalar(db, "SELECT audit_allowlist('listing_repair')") == ["rule", "attribute"]
    order = scalar(db, "SELECT purge_tenant_order()")
    assert order.index("listing_repairs") < order.index("listing_items")


# ---- marketplace_error_catalog (T2) ----

CATALOG_COLUMNS = {
    "marketplace",
    "marketplace_code",
    "error_code",
    "error_class",
    "mapped",
    "issues",
    "occurrences",
    "tenants",
    "first_seen",
    "last_seen",
    "preview",
    "resolutions",
    "recurrence_rate",
}


def _adapter_issue(db: Conn, fl: Any, **columns: Any) -> Any:
    values: dict[str, Any] = {
        "source": "amazon_adapter",
        "field_path": "facts.attributes.color",
        "error_code": "INVALID_VALUE",
        "error_class": "invalid_value",
        "retry_class": "user_action",
        "marketplace_code": "77777",
        "raw_message": "secret seller text",
        "message": "The colour is not one Amazon accepts.",
        **columns,
    }
    values.setdefault(
        "dedupe_key",
        f"{values['source']}|{values['field_path']}|{values['error_code']}|{values['marketplace_code']}",
    )
    return f.listing_issue(db, fl.tenant_id, fl.listing_id, **values)


def test_catalog_columns_have_no_tenant_or_text(db: Conn) -> None:
    columns = {
        str(r[0])
        for r in db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'marketplace_error_catalog'"
        )
    }
    assert columns == CATALOG_COLUMNS
    assert not columns & {"tenant_id", "raw_message", "message", "plain_message"}


def test_catalog_counts_across_tenants(db: Conn) -> None:
    a, b = flow(db), flow(db)
    _adapter_issue(db, a, marketplace_code="99901")
    _adapter_issue(db, b, marketplace_code="99901", occurrences=3)
    _adapter_issue(
        db, b, marketplace_code="99901", field_path="facts.attributes.size",
        dedupe_key="amazon_adapter|facts.attributes.size|INVALID_VALUE|99901|preview",
    )  # fmt: skip
    f.listing_issue(db, a.tenant_id, a.listing_id)  # a rules_engine row never counts
    with db.transaction():
        db.execute("SET LOCAL ROLE app_platform_ro")
        row = db.execute(
            "SELECT mapped, issues, occurrences, tenants, preview, resolutions, recurrence_rate "
            "FROM marketplace_error_catalog WHERE marketplace_code = '99901'"
        ).fetchall()
    assert len(row) == 1
    mapped, issues, occurrences, tenants, preview, resolutions, rate = row[0]
    assert (mapped, issues, occurrences, tenants, preview) == (False, 3, 5, 2, True)
    assert resolutions == {"open": 3}
    assert float(rate) == 0.0


def test_catalog_refused_for_app_rw(db: Conn) -> None:
    with db.transaction():
        db.execute("SET LOCAL ROLE app_rw")
        expect_sqlstate(db, "42501", "SELECT count(*) FROM marketplace_error_catalog")


def test_catalog_app_ro_reads_only_through_platform_ro(db: Conn) -> None:
    """app_ro holds no grant of its own; it reads the view only because it inherits
    app_platform_ro (0000_bootstrap: `GRANT app_platform_ro TO app_ro WITH INHERIT TRUE`)."""
    grants = {
        str(r[0])
        for r in db.execute(
            "SELECT grantee FROM information_schema.role_table_grants "
            "WHERE table_name = 'marketplace_error_catalog'"
        )
    }
    assert grants - {"migrator"} == {"app_platform_ro"}


def test_downgrade_refused_with_repair_row(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        conn.execute("SET session_replication_role = replica")
        fl = flow(conn)
        f.listing_repair(conn, fl.tenant_id, fl.listing_id)
        conn.commit()
    with pytest.raises(Exception, match="downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION
