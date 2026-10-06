"""0004/04_lv_guard (DATABASE.md §5.3 listing_versions :786-789; BA:2065-2071): maker-checker at the
database. Raw SQL as `app_rw` (or the owner, where the guard alone is under test)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg import sql

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate, head_database
from .integrity_support import APPROVE_SQL, approve, as_app_rw, next_version, tenant_of

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def _setup(db: Conn) -> tuple[UUID, UUID, UUID, UUID]:
    """A tenant, its listing, the listing's current generation version and its generator."""
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    generator = f.member(db, tenant, role="operator")
    version = next_version(db, tenant, listing, None, "generation", generator)
    return tenant, listing, version, generator


def _refused(db: Conn, version: UUID, approver: UUID, code: str = "MKC01") -> None:
    with as_app_rw(db, tenant_of(db, "listing_versions", version)):
        expect_sqlstate(db, code, APPROVE_SQL, (approver, version))


def test_db_maker_checker_generator(db: Conn) -> None:
    tenant, listing, _, _ = _setup(db)
    owner = f.member(db, tenant, role="owner")
    version = next_version(db, tenant, listing, None, "generation", owner)
    _refused(db, version, owner)


def test_db_maker_checker_editor(db: Conn) -> None:
    tenant, listing, v1, generator = _setup(db)
    editor = f.member(db, tenant, role="reviewer")
    v2 = next_version(db, tenant, listing, v1, "edit", generator, edited_by=editor)
    _refused(db, v2, editor)


def test_db_maker_checker_cross_tenant_fk(db: Conn) -> None:
    _, _, version, _ = _setup(db)
    stranger = f.member(db, f.tenant(db), role="reviewer")
    _refused(db, version, stranger, code="23503")


def test_db_maker_checker_inactive(db: Conn) -> None:
    tenant, _, version, _ = _setup(db)
    reviewer = f.member(db, tenant, role="reviewer", is_active=False)
    _refused(db, version, reviewer)


def test_db_maker_checker_admin(db: Conn) -> None:
    tenant, _, version, _ = _setup(db)
    _refused(db, version, f.member(db, tenant, role="admin"))


def test_db_maker_checker_operator(db: Conn) -> None:
    tenant, _, version, _ = _setup(db)
    _refused(db, version, f.member(db, tenant, role="operator"))


def test_db_maker_checker_non_current_version(db: Conn) -> None:
    tenant, listing, v1, generator = _setup(db)
    next_version(db, tenant, listing, v1, "regeneration", generator)
    _refused(db, v1, f.member(db, tenant, role="reviewer"))


def test_db_maker_checker_contributor_chain_partial_regen(db: Conn) -> None:
    tenant, listing, v1, generator = _setup(db)
    editor = f.member(db, tenant, role="reviewer")
    v2 = next_version(db, tenant, listing, v1, "edit", generator, edited_by=editor)
    regenerator = f.member(db, tenant, role="operator")
    v3 = next_version(
        db, tenant, listing, v2, "regeneration", regenerator, changed_paths=["content.title"]
    )
    _refused(db, v3, editor)
    _refused(db, v3, generator)


def test_db_maker_checker_chain_reset_by_full_regen(db: Conn) -> None:
    tenant, listing, v1, generator = _setup(db)
    editor = f.member(db, tenant, role="reviewer")
    v2 = next_version(db, tenant, listing, v1, "edit", generator, edited_by=editor)
    regenerator = f.member(db, tenant, role="operator")
    v3 = next_version(db, tenant, listing, v2, "regeneration", regenerator)
    with as_app_rw(db, tenant):
        approve(db, v3, editor)
    row = db.execute(
        "SELECT approved_by_user_id, approved_at = now() FROM listing_versions WHERE id = %s",
        (v3,),
    ).fetchone()
    assert row == (editor, True)


def test_db_maker_checker_approval_set_once(db: Conn) -> None:
    tenant, _, version, _ = _setup(db)
    first, second = f.member(db, tenant, role="reviewer"), f.member(db, tenant, role="owner")
    with as_app_rw(db, tenant):
        approve(db, version, first)
    _refused(db, version, second, code="GRD01")
    with as_app_rw(db, tenant):
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE listing_versions SET approved_by_user_id = NULL, approved_at = NULL "
            "WHERE id = %s",
            (version,),
        )
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE listing_versions SET approved_at = now() - interval '1 day' WHERE id = %s",
            (version,),
        )


@pytest.mark.parametrize(
    "assignment",
    [
        'content_json = \'{"title": "changed"}\'',
        "locale = 'hi-IN'",
        "rules_version = 7",
        "attempts_used = 1",
        "edited_by_user_id = generated_by_user_id",
    ],
)
def test_db_maker_checker_content_immutable(db: Conn, assignment: str) -> None:
    _, _, version, _ = _setup(db)
    query = sql.SQL("UPDATE listing_versions SET {} WHERE id = %s").format(sql.SQL(assignment))
    expect_sqlstate(db, "GRD01", query, (version,))  # the owner: the trigger alone
    with as_app_rw(db):
        expect_sqlstate(db, "42501", query, (version,))  # app_rw: no column privilege


def test_db_maker_checker_reviewer_passes(db: Conn) -> None:
    tenant, _, version, _ = _setup(db)
    approver = f.member(db, tenant, role="reviewer")
    with as_app_rw(db, tenant):
        approve(db, version, approver)


def test_db_maker_checker_owner_passes(db: Conn) -> None:
    tenant, _, version, _ = _setup(db)
    approver = f.member(db, tenant, role="owner")
    with as_app_rw(db, tenant):
        approve(db, version, approver)


def test_db_maker_checker_no_delete(db: Conn) -> None:
    _, _, version, _ = _setup(db)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "DELETE FROM listing_versions WHERE id = %s", (version,))


# ---- an INSERT never carries an approval (the approval is only the guarded UPDATE) ----

INSERT_APPROVED = (
    "INSERT INTO listing_versions (tenant_id, listing_id, version, content_json, origin, "
    "parent_version_id, generated_by_user_id, changed_paths, approved_by_user_id, approved_at) "
    'VALUES (%s, %s, %s, \'{"title": "t"}\', %s, %s, %s, %s, %s, %s)'
)


def _preapproved_cases(db: Conn) -> dict[str, tuple[Any, ...]]:
    """Row values up to (and including) the approver, one per kind of would-be approver."""
    tenant, listing, v1, generator = _setup(db)
    editor = f.member(db, tenant, role="reviewer")
    v2 = next_version(db, tenant, listing, v1, "edit", generator, edited_by=editor)
    regenerator = f.member(db, tenant, role="operator")
    cases: dict[str, tuple[Any, ...]] = {}
    for name, role, active in (
        ("operator", "operator", True),
        ("admin", "admin", True),
        ("inactive reviewer", "reviewer", False),
        ("active reviewer", "reviewer", True),
    ):
        approver = f.member(db, tenant, role=role, is_active=active)
        cases[name] = (tenant, listing, 90, "generation", None, regenerator, None, approver)
    # a partial regeneration carries v2's edit forward: its editor is an earlier contributor
    cases["earlier contributor"] = (
        tenant,
        listing,
        90,
        "regeneration",
        v2,
        regenerator,
        ["content.title"],
        editor,
    )
    return cases


@pytest.mark.parametrize("columns", ["both", "approved_by only", "approved_at only"])
def test_db_maker_checker_insert_preapproved(db: Conn, columns: str) -> None:
    """The trigger path (as the owner, who holds every column): any approval on INSERT → GRD01,
    whoever the approver is, so the approver, current-version and chain checks cannot be
    skipped (the guard runs before ck_approval_ts and ck_maker_checker)."""
    for name, (*values, approver) in _preapproved_cases(db).items():
        by = None if columns == "approved_at only" else approver
        at = None if columns == "approved_by only" else "2026-10-05"
        err = expect_sqlstate(db, "GRD01", INSERT_APPROVED, (*values, by, at))
        assert "inserted unapproved" in str(err), name


def test_db_maker_checker_insert_preapproved_app_rw(db: Conn) -> None:
    """The privilege path: app_rw holds no INSERT on the two approval columns (42501), while an
    unapproved INSERT of the other columns still works."""
    for name, (*values, approver) in _preapproved_cases(db).items():
        with as_app_rw(db, values[0]):
            err = expect_sqlstate(db, "42501", INSERT_APPROVED, (*values, approver, "2026-10-05"))
        assert "permission denied" in str(err), name
    tenant, listing, _, generator = _setup(db)
    with as_app_rw(db, tenant):
        db.execute(
            "INSERT INTO listing_versions (tenant_id, listing_id, version, content_json, origin, "
            "generated_by_user_id) VALUES (%s, %s, 7, '{\"title\": \"t\"}', 'generation', %s)",
            (tenant, listing, generator),
        )


# ---- two connections (DATABASE.md:1792 "approve vs deactivate") ----


@pytest.fixture(scope="module")
def race_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("lv_race")) as scratch:
        yield scratch


def test_db_maker_checker_race_approve_vs_deactivate(race_db: ScratchDb, tmp_path: Path) -> None:
    """The deactivation commits while the approval waits on the membership row (FOR SHARE): the
    approval then reads the committed row and is refused, never approves a deactivated user."""
    with psycopg.connect(race_db.libpq, autocommit=True) as setup:
        tenant, _, version, _ = _setup(setup)
        reviewer = f.member(setup, tenant, role="reviewer")
    result: dict[str, Any] = {}
    with psycopg.connect(race_db.libpq) as deactivator:
        deactivator.execute(
            "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
            "WHERE tenant_id = %s AND user_id = %s",
            (tenant, reviewer),
        )

        def run_approve() -> None:
            with psycopg.connect(race_db.libpq) as approver:
                approver.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
                approver.execute("SET ROLE app_rw")
                try:
                    approve(approver, version, reviewer)
                    approver.commit()
                    result["outcome"] = "approved"
                except psycopg.Error as error:
                    result["outcome"] = error.sqlstate

        thread = threading.Thread(target=run_approve)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the approval did not wait for the membership row"
        deactivator.commit()
        thread.join(timeout=10)
    assert result == {"outcome": "MKC01"}


# ---- property (Hypothesis): refused ⇔ approver ∈ contributors since the last reset ----

STEP = st.tuples(
    st.sampled_from(["edit", "partial", "full", "generation"]),
    st.integers(min_value=0, max_value=3),
    st.integers(min_value=0, max_value=3),
)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    first=st.integers(min_value=0, max_value=3),
    steps=st.lists(STEP, max_size=5),
    approver=st.integers(min_value=0, max_value=3),
)
def test_db_maker_checker_property(
    db: Conn, first: int, steps: list[tuple[str, int, int]], approver: int
) -> None:
    with db.transaction(force_rollback=True):
        tenant = f.tenant(db)
        listing = f.listing(db, tenant)
        users = [f.member(db, tenant, role="reviewer") for _ in range(4)]
        version = next_version(db, tenant, listing, None, "generation", users[first])
        contributors = {users[first]}
        for kind, maker, editor in steps:
            if kind == "edit":
                version = next_version(
                    db, tenant, listing, version, "edit", users[maker], edited_by=users[editor]
                )
                contributors |= {users[maker], users[editor]}
            elif kind == "partial":
                version = next_version(
                    db,
                    tenant,
                    listing,
                    version,
                    "regeneration",
                    users[maker],
                    changed_paths=["content.bullets"],
                )
                contributors |= {users[maker]}
            else:
                origin = "regeneration" if kind == "full" else "generation"
                version = next_version(db, tenant, listing, version, origin, users[maker])
                contributors = {users[maker]}
        refused = users[approver] in contributors
        try:
            with as_app_rw(db, tenant), db.transaction():
                approve(db, version, users[approver])
        except psycopg.Error as error:
            assert refused and error.sqlstate == "MKC01", error
        else:
            assert not refused
