"""Round-2 tester attacks on the S10a fixes (0004/04, 12, 15, 18): the INSERT side of lv_guard,
fail-closed DELETE GUCs, the publish_field_results ledger and the purge preconditions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, flow, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


# ---- lv_guard: approval columns cannot ride in on an INSERT path ----


def test_lv_upsert_cannot_self_approve(db: Conn) -> None:
    """INSERT .. ON CONFLICT DO UPDATE of the approval runs the UPDATE branch: the maker of v1
    cannot approve it through an upsert (MKC01)."""
    fl = flow(db)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "MKC01",
            "INSERT INTO listing_versions (id, tenant_id, listing_id, version, content_json, "
            "origin, generated_by_user_id) VALUES (%s, %s, %s, 1, '{}', 'generation', %s) "
            "ON CONFLICT (id) DO UPDATE SET approved_by_user_id = EXCLUDED.generated_by_user_id",
            (fl.v1, fl.tenant_id, fl.listing_id, fl.operator),
        )


def test_lv_app_rw_insert_grant_is_every_column_but_approval(db: Conn) -> None:
    """A column added to listing_versions later must not silently fall outside app_rw's INSERT."""
    rows = db.execute(
        "SELECT column_name, has_column_privilege('app_rw', 'public.listing_versions', "
        "column_name, 'INSERT') FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'listing_versions'"
    ).fetchall()
    denied = {name for name, ok in rows if not ok}
    assert denied == {"approved_by_user_id", "approved_at"}


# ---- DELETE allow-tests fail closed for every GUC shape ----


@pytest.mark.parametrize(
    "value", ["", "   ", "not_a_table", "00000000-0000-0000-0000-000000000000"]
)
def test_garbage_prune_guc_never_deletes(db: Conn, value: str) -> None:
    tenant = f.tenant(db)
    issue = f.listing_issue(db, tenant, f.listing(db, tenant))
    db.execute(
        "UPDATE listing_issues SET status = 'resolved', resolution = 'human_edit', "
        "resolved_at = now() WHERE id = %s",
        (issue,),
    )
    with as_migrator(db):
        db.execute("SAVEPOINT g")
        set_gucs(db, prune_table=value)
        with pytest.raises(psycopg.Error):
            db.execute("DELETE FROM listing_issues WHERE id = %s", (issue,))
        db.execute("ROLLBACK TO SAVEPOINT g")
    assert db.execute("SELECT count(*) FROM listing_issues WHERE id = %s", (issue,)).fetchone() == (
        1,
    )


def test_rules_delete_platform_rule_refused_even_under_purge(db: Conn) -> None:
    """A platform rule (NULL tenant) is never deleted, whatever tenant the purge GUC names."""
    rule = f.rule(db)
    with as_migrator(db):
        set_gucs(db, purge_tenant=f.tenant(db))
        expect_sqlstate(db, "GRD01", "DELETE FROM rules WHERE id = %s", (rule,))


# ---- publish_field_results: append-only for app_rw ----


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE", "TRUNCATE"])
def test_publish_field_results_append_only_for_app_rw(db: Conn, statement: str) -> None:
    query = {
        "UPDATE": "UPDATE publish_field_results SET tenant_id = tenant_id",
        "DELETE": "DELETE FROM publish_field_results",
        "TRUNCATE": "TRUNCATE publish_field_results",
    }[statement]
    with as_app_rw(db):
        expect_sqlstate(db, "42501", query)


# ---- purge_tenant preconditions ----


def _tenant_with_listing(db: Conn) -> UUID:
    tenant = f.tenant(db)
    f.listing(db, tenant)
    return tenant


def _listings(db: Conn, tenant: UUID) -> int:
    row = db.execute("SELECT count(*) FROM listings WHERE tenant_id = %s", (tenant,)).fetchone()
    assert row is not None
    return int(row[0])


def test_purge_force_null_is_not_force(db: Conn) -> None:
    tenant = _tenant_with_listing(db)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'data', NULL)", (tenant,))
    assert _listings(db, tenant) == 1


def test_purge_offboarded_with_null_purge_after_refused(db: Conn) -> None:
    tenant = _tenant_with_listing(db)
    db.execute("SET LOCAL session_replication_role = replica")
    db.execute(
        "UPDATE tenants SET status = 'offboarded', offboarded_at = now(), purge_after = NULL "
        "WHERE id = %s",
        (tenant,),
    )
    db.execute("SET LOCAL session_replication_role = origin")
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'data')", (tenant,))
    assert _listings(db, tenant) == 1


def test_purge_force_keeps_data_before_final(db: Conn) -> None:
    tenant = _tenant_with_listing(db)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'final', force => true)", (tenant,))
    assert _listings(db, tenant) == 1


def test_purge_final_rejected_right_after_data_even_if_offboarded_long_ago(db: Conn) -> None:
    """The retention clock is data_purged_at, not offboarded_at or purge_after."""
    tenant = _tenant_with_listing(db)
    with as_migrator(db):
        db.execute("SELECT set_tenant_lifecycle(%s, 'offboarded', 'test')", (tenant,))
    db.execute(
        "UPDATE tenants SET purge_after = now() - interval '5 years', "
        "offboarded_at = now() - interval '5 years' WHERE id = %s",
        (tenant,),
    )
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data')", (tenant,))
        expect_sqlstate(db, "GRD01", "SELECT purge_tenant(%s, 'final')", (tenant,))
    assert db.execute("SELECT count(*) FROM tenants WHERE id = %s", (tenant,)).fetchone() == (1,)


def test_purge_not_executable_by_app_roles(db: Conn) -> None:
    for role in ("app_rw", "app_ro", "app_platform_ro"):
        row = db.execute(
            "SELECT has_function_privilege(%s, 'public.purge_tenant(uuid,text,boolean)', "
            "'EXECUTE')",
            (role,),
        ).fetchone()
        assert row == (False,), role
