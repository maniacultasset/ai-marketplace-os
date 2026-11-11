"""Revision 0030_owner_self_approval (ADR-0031, owner decision 2026-11-11): the single head, the
two columns, the `lv_guard` backstop that lets exactly one more case through (an active owner in
the contributor chain while the tenant's `owner_self_approval` is on), and the guarded downgrade.
Raw SQL as `app_rw`, as in `test_0004_lv_guard.py`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import APPROVE_SQL, approve, as_app_rw, next_version, scalar, tenant_of

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0028_photo_read_on_upload"
REVISION = "0030_owner_self_approval"


def _setting(db: Conn, tenant: UUID, on: bool) -> None:
    db.execute("UPDATE tenants SET owner_self_approval = %s WHERE id = %s", (on, tenant))


def _refused(db: Conn, version: UUID, approver: UUID, code: str = "MKC01") -> None:
    with as_app_rw(db, tenant_of(db, "listing_versions", version)):
        expect_sqlstate(db, code, APPROVE_SQL, (approver, version))


def _approves(db: Conn, version: UUID, approver: UUID, *, own: bool = True) -> None:
    """The approval goes through, and lv_guard marks it a self-approval exactly when `own`."""
    with as_app_rw(db, tenant_of(db, "listing_versions", version)):
        approve(db, version, approver)
    row = db.execute(
        "SELECT approved_by_user_id, self_approved FROM listing_versions WHERE id = %s", (version,)
    ).fetchone()
    assert row == (approver, own)


def _own(db: Conn, role: str = "owner") -> tuple[UUID, UUID, UUID]:
    """A tenant, a member of `role` and the current generation version that member made."""
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    maker = f.member(db, tenant, role=role)
    return tenant, maker, next_version(db, tenant, listing, None, "generation", maker)


# ---- the columns ----


def test_columns_default_off(db: Conn) -> None:
    rows = db.execute(
        "SELECT table_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE (table_name, column_name) IN (('tenants', 'owner_self_approval'), "
        "('listing_versions', 'self_approved')) ORDER BY table_name"
    ).fetchall()
    assert rows == [
        ("listing_versions", "boolean", "NO", "false"),
        ("tenants", "boolean", "NO", "false"),
    ]
    assert scalar(db, "SELECT owner_self_approval FROM tenants WHERE id = %s", (f.tenant(db),)) is (
        False
    )


def test_privileges(db: Conn) -> None:
    """app_rw changes the setting but never writes `self_approved` (only lv_guard does); the
    read-only roles read both."""
    privilege = "SELECT has_column_privilege(%s, %s, %s, %s)"
    assert scalar(db, privilege, ("app_rw", "public.tenants", "owner_self_approval", "UPDATE"))
    for kind in ("INSERT", "UPDATE"):
        assert not scalar(
            db, privilege, ("app_rw", "public.listing_versions", "self_approved", kind)
        )
    for role in ("app_ro", "app_platform_ro"):
        assert scalar(db, privilege, (role, "public.tenants", "owner_self_approval", "SELECT"))
        assert scalar(db, privilege, (role, "public.listing_versions", "self_approved", "SELECT"))


def test_app_rw_cannot_claim_a_self_approval(db: Conn) -> None:
    tenant, owner, version = _own(db)
    _setting(db, tenant, True)
    with as_app_rw(db, tenant):
        expect_sqlstate(
            db,
            "42501",
            "UPDATE listing_versions SET approved_by_user_id = %s, self_approved = true "
            "WHERE id = %s",
            (owner, version),
        )


def test_self_approved_is_written_by_the_guard_only(db: Conn) -> None:
    """As the table owner (every column held): an INSERT with the flag, a flag without an
    approval, and a later flip of an approval's flag are each GRD01."""
    tenant, owner, version = _own(db)
    listing = scalar(db, "SELECT listing_id FROM listing_versions WHERE id = %s", (version,))
    expect_sqlstate(
        db, "GRD01", "UPDATE listing_versions SET self_approved = true WHERE id = %s", (version,)
    )
    with pytest.raises(psycopg.Error) as err, db.transaction():
        f.version(db, tenant, listing, generated_by_user_id=owner, self_approved=True)
    assert err.value.sqlstate == "GRD01"
    other = f.member(db, tenant, role="reviewer")
    _approves(db, version, other, own=False)
    expect_sqlstate(
        db, "GRD01", "UPDATE listing_versions SET self_approved = true WHERE id = %s", (version,)
    )


def test_ck_maker_checker_admits_only_a_marked_self_approval(db: Conn) -> None:
    definition = str(
        scalar(
            db,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_maker_checker'",
        )
    )
    assert "self_approved" in definition and "generated_by_user_id" in definition
    assert "approved_by_user_id IS NOT NULL" in str(
        scalar(
            db,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_listing_versions_self_approved'",
        )
    )


def test_audit_allowlist_keeps_the_setting(db: Conn) -> None:
    assert set(scalar(db, "SELECT audit_allowlist('tenant')")) == {
        "name",
        "tone",
        "guidelines",
        "glossary",
        "owner_self_approval",
    }


# ---- lv_guard: exactly one more case ----


def test_owner_own_work_refused_with_the_setting_off(db: Conn) -> None:
    _, owner, version = _own(db)
    _refused(db, version, owner)


def test_owner_own_work_approved_with_the_setting_on(db: Conn) -> None:
    tenant, owner, version = _own(db)
    _setting(db, tenant, True)
    _approves(db, version, owner)


def test_owner_as_editor_and_earlier_contributor_with_the_setting_on(db: Conn) -> None:
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    operator = f.member(db, tenant, role="operator")
    owner = f.member(db, tenant, role="owner")
    v1 = next_version(db, tenant, listing, None, "generation", operator)
    v2 = next_version(db, tenant, listing, v1, "edit", operator, edited_by=owner)
    v3 = next_version(db, tenant, listing, v2, "edit", operator, edited_by=operator)
    _refused(db, v3, owner)  # the earlier editor, setting off
    _setting(db, tenant, True)
    _approves(db, v3, owner)


@pytest.mark.parametrize("setting", [True, False])
def test_reviewer_own_work_always_refused(db: Conn, setting: bool) -> None:
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    operator = f.member(db, tenant, role="operator")
    reviewer = f.member(db, tenant, role="reviewer")
    v1 = next_version(db, tenant, listing, None, "generation", operator)
    v2 = next_version(db, tenant, listing, v1, "edit", operator, edited_by=reviewer)
    v3 = next_version(
        db, tenant, listing, v2, "regeneration", operator, changed_paths=["content.title"]
    )
    _setting(db, tenant, setting)
    _refused(db, v3, reviewer)  # the earlier editor through a partial regeneration
    tenant_r, maker_r, own = _own(db, role="reviewer")  # a reviewer's full regeneration
    _setting(db, tenant_r, setting)
    _refused(db, own, maker_r)


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_operator_and_admin_never_approve_under_the_setting(db: Conn, role: str) -> None:
    tenant, maker, version = _own(db, role=role)
    _setting(db, tenant, True)
    _refused(db, version, maker)
    _refused(db, version, f.member(db, tenant, role=role))


def test_inactive_or_unverified_owner_refused_under_the_setting(db: Conn) -> None:
    tenant, owner, version = _own(db)
    _setting(db, tenant, True)
    db.execute("UPDATE users SET email_unverified = true WHERE id = %s", (owner,))
    _refused(db, version, owner)
    db.execute("UPDATE users SET email_unverified = false WHERE id = %s", (owner,))
    second = f.member(db, tenant, role="owner", is_active=False)
    listing = scalar(db, "SELECT listing_id FROM listing_versions WHERE id = %s", (version,))
    theirs = next_version(db, tenant, listing, version, "edit", owner, edited_by=second)
    _refused(db, theirs, second)
    _approves(db, theirs, owner)  # the active owner, an earlier contributor, under the setting


def test_setting_of_another_tenant_never_counts(db: Conn) -> None:
    _, owner, version = _own(db)
    _setting(db, f.tenant(db), True)
    _refused(db, version, owner)


def test_self_approval_still_needs_the_current_version(db: Conn) -> None:
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    owner = f.member(db, tenant, role="owner")
    v1 = next_version(db, tenant, listing, None, "generation", owner)
    next_version(db, tenant, listing, v1, "regeneration", owner)
    _setting(db, tenant, True)
    _refused(db, v1, owner)


def test_self_approval_is_still_set_once(db: Conn) -> None:
    tenant, owner, version = _own(db)
    _setting(db, tenant, True)
    _approves(db, version, owner)
    _refused(db, version, f.member(db, tenant, role="owner"), code="GRD01")


def test_turning_the_setting_off_restores_the_rule(db: Conn) -> None:
    tenant, owner, version = _own(db)
    _setting(db, tenant, True)
    _setting(db, tenant, False)
    _refused(db, version, owner)


# ---- the setting and an approval serialize (lv_guard reads it FOR SHARE) ----


@pytest.fixture
def committed_owner(head_db: ScratchDb) -> Iterator[tuple[UUID, UUID, UUID]]:
    """A committed tenant, its owner and the owner's current version, setting on; purged after
    (`head_db` is shared by the whole session)."""
    with psycopg.connect(head_db.libpq, autocommit=True, connect_timeout=5) as conn:
        tenant, owner, version = _own(conn)
        _setting(conn, tenant, True)
    yield tenant, owner, version
    with psycopg.connect(head_db.libpq, autocommit=True, connect_timeout=5) as conn:
        conn.execute("SET SESSION AUTHORIZATION migrator")
        for mode in ("data", "final"):
            conn.execute("SELECT purge_tenant(%s, %s, force => true)", (tenant, mode))


def _short_wait(conn: Conn) -> None:
    conn.execute("SET lock_timeout = '300ms'")


def test_uncommitted_toggle_off_holds_the_approval(
    head_db: ScratchDb, committed_owner: tuple[UUID, UUID, UUID]
) -> None:
    """A toggle-off still open: the self-approval waits for it (lock timeout here) rather than
    reading the old "on"; once the toggle commits, the approval sees "off" and is MKC01."""
    tenant, owner, version = committed_owner
    with (
        psycopg.connect(head_db.libpq, connect_timeout=5) as toggle,
        psycopg.connect(head_db.libpq, connect_timeout=5) as approver,
    ):
        _setting(toggle, tenant, False)  # uncommitted
        _short_wait(approver)
        with as_app_rw(approver, tenant):
            expect_sqlstate(approver, "55P03", APPROVE_SQL, (owner, version))
            toggle.commit()
            expect_sqlstate(approver, "MKC01", APPROVE_SQL, (owner, version))
        approver.rollback()


def test_approval_in_flight_holds_the_toggle(
    head_db: ScratchDb, committed_owner: tuple[UUID, UUID, UUID]
) -> None:
    """A self-approval still open holds the setting: a toggle waits (lock timeout here) until
    the approval commits, so the owner never sees "off" and then a new self-approval land."""
    tenant, owner, version = committed_owner
    with (
        psycopg.connect(head_db.libpq, connect_timeout=5) as toggle,
        psycopg.connect(head_db.libpq, connect_timeout=5) as approver,
    ):
        with as_app_rw(approver, tenant):
            approve(approver, version, owner)  # uncommitted
        _short_wait(toggle)
        expect_sqlstate(
            toggle,
            "55P03",
            "UPDATE tenants SET owner_self_approval = false WHERE id = %s",
            (tenant,),
        )
        approver.commit()
        _setting(toggle, tenant, False)
        toggle.commit()
        row = toggle.execute(
            "SELECT approved_by_user_id, self_approved FROM listing_versions WHERE id = %s",
            (version,),
        ).fetchone()
        assert row == (owner, True)


# ---- the chain and the round trip ----


def test_single_head_on_0028() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def _guard_body(libpq: str) -> str:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return str(scalar(conn, "SELECT prosrc FROM pg_proc WHERE proname = 'lv_guard'"))


def _has_setting(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return bool(
            scalar(
                conn,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'tenants' AND column_name = 'owner_self_approval'",
            )
        )


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _guard_body(scratch_db.libpq)
    assert "owner_self_approval" not in before and not _has_setting(scratch_db.libpq)
    command.upgrade(cfg, REVISION)
    assert "owner_self_approval" in _guard_body(scratch_db.libpq)
    assert _has_setting(scratch_db.libpq)
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert _guard_body(scratch_db.libpq) == before  # 0026's body, verbatim
    assert not _has_setting(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        body = str(scalar(conn, "SELECT prosrc FROM pg_proc WHERE proname = 'audit_allowlist'"))
    assert "owner_self_approval" not in body and "marketplace_error_proposal" in body
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_refused_while_a_self_approval_is_recorded(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        # a scratch database has no dictionary: the fact guard is off for this set-up only (the
        # approval itself still goes through lv_guard)
        conn.execute("ALTER TABLE products DISABLE TRIGGER facts_guard")
        tenant = f.tenant(conn)
        listing = f.listing(conn, tenant)
        owner = f.member(conn, tenant, role="owner")
        version = next_version(conn, tenant, listing, None, "generation", owner)
        _setting(conn, tenant, True)
        _approves(conn, version, owner)
        conn.commit()
        conn.execute("ALTER TABLE products ENABLE TRIGGER facts_guard")
        conn.commit()
    with pytest.raises(Exception, match="0030 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    assert _has_setting(scratch_db.libpq)
