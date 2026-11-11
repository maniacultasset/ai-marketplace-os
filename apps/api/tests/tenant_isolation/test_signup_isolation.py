"""ADR-0019 (0010): `signup_requests` and `users.is_platform_admin` as the raw `app_rw` login,
with no repository or ORM guard in the way.

An applicant reads only their own requests and inserts only a fresh pending one for themselves;
a member who is not a platform admin reads and changes no request; with no GUC the table is
empty; a platform admin reads and decides every request; app_rw can neither set the admin flag
(the trigger) nor delete a request (the privilege layer)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
import pytest

Conn = psycopg.Connection[tuple[Any, ...]]
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"


@dataclass(frozen=True)
class SignupWorld:
    applicant_a: UUID
    applicant_b: UUID
    member: UUID
    admin: UUID
    tenant: UUID
    request_a: UUID
    request_b: UUID


def _user(conn: Conn, email: str, *, admin: bool = False) -> UUID:
    row = conn.execute(
        "INSERT INTO users (email, hashed_password, is_platform_admin) VALUES (%s, %s, %s) "
        "RETURNING id",
        [email, HASH, admin],
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def _request(conn: Conn, user: UUID, brand: str) -> UUID:
    row = conn.execute(
        "INSERT INTO signup_requests (user_id, brand_name, owner_name, phone) "
        "VALUES (%s, %s, 'Owner', '+91 90000 00000') RETURNING id",
        [user, brand],
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> SignupWorld:
    conn = clean_module_admin_conn
    tenant = conn.execute(
        "INSERT INTO tenants (name, slug, status) VALUES ('Acme', 'acme', 'active') RETURNING id"
    ).fetchone()[
        0
    ]  # type: ignore[index]
    a = _user(conn, "a@apply.test")
    b = _user(conn, "b@apply.test")
    member = _user(conn, "owner@acme.test")
    admin = _user(conn, "ops@acme.test", admin=True)
    for user, role in ((member, "owner"), (admin, "operator")):
        conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, %s)",
            [tenant, user, role],
        )
    return SignupWorld(a, b, member, admin, tenant, _request(conn, a, "A"), _request(conn, b, "B"))


@contextmanager
def as_user(conn: Conn, user: UUID | None, tenant: UUID | None = None) -> Iterator[Conn]:
    with conn.transaction(force_rollback=True):
        if user is not None:
            conn.execute("SELECT set_config('app.user_id', %s, true)", [str(user)])
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
        yield conn


def _ids(conn: Conn) -> set[UUID]:
    return {r[0] for r in conn.execute("SELECT id FROM signup_requests").fetchall()}


def test_no_guc_reads_no_request(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, None):
        assert _ids(app_conn) == set()


def test_applicant_reads_only_own_request(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.applicant_a):
        assert _ids(app_conn) == {world.request_a}
    with as_user(app_conn, world.applicant_b):
        assert _ids(app_conn) == {world.request_b}


def test_member_in_a_tenant_reads_and_changes_no_request(
    app_conn: Conn, admin_conn: Conn, world: SignupWorld
) -> None:
    with as_user(app_conn, world.member, world.tenant):
        assert _ids(app_conn) == set()
        updated = app_conn.execute(
            "UPDATE signup_requests SET status = 'approved', decided_at = now()"
        )
        assert updated.rowcount == 0
    statuses = admin_conn.execute("SELECT DISTINCT status FROM signup_requests").fetchall()
    assert statuses == [("pending",)]


def test_applicant_cannot_decide_own_request(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.applicant_a):
        updated = app_conn.execute(
            "UPDATE signup_requests SET status = 'approved', decided_at = now() WHERE id = %s",
            [world.request_a],
        )
        assert updated.rowcount == 0


def test_platform_admin_reads_every_request_and_applicant(
    app_conn: Conn, world: SignupWorld
) -> None:
    with as_user(app_conn, world.admin, world.tenant):
        assert _ids(app_conn) >= {world.request_a, world.request_b}
        emails = {
            r[0]
            for r in app_conn.execute(
                "SELECT u.email FROM signup_requests r JOIN users u ON u.id = r.user_id"
            ).fetchall()
        }
        assert {"a@apply.test", "b@apply.test"} <= emails
        updated = app_conn.execute(
            "UPDATE signup_requests SET reason = reason WHERE id = %s", [world.request_a]
        )
        assert updated.rowcount == 1


def test_inactive_admin_is_no_admin(app_conn: Conn, admin_conn: Conn, world: SignupWorld) -> None:
    admin_conn.execute("UPDATE users SET is_active = false WHERE id = %s", [world.admin])
    try:
        with as_user(app_conn, world.admin, world.tenant):
            assert _ids(app_conn) == set()
    finally:
        admin_conn.execute("UPDATE users SET is_active = true WHERE id = %s", [world.admin])


@pytest.mark.parametrize(
    "values",
    [
        "(%(other)s, 'X', 'Owner', 'pending', NULL)",  # someone else's user id
        "(%(self)s, 'X', 'Owner', 'approved', NULL)",  # not a fresh pending row
        "(%(self)s, 'X', 'Owner', 'pending', 'pre-decided')",
    ],
)
def test_insert_other_user_or_decided_row_refused(
    app_conn: Conn, world: SignupWorld, values: str
) -> None:
    sql = (
        "INSERT INTO signup_requests (user_id, brand_name, owner_name, status, reason) VALUES "  # noqa: S608 - fixed literals
        + values
    )
    with (
        as_user(app_conn, world.applicant_b),
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"),
    ):
        app_conn.execute(sql, {"self": world.applicant_b, "other": world.applicant_a})


def test_one_pending_request_per_user(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.applicant_a), pytest.raises(psycopg.errors.UniqueViolation):
        app_conn.execute(
            "INSERT INTO signup_requests (user_id, brand_name, owner_name) VALUES (%s, 'A2', 'O')",
            [world.applicant_a],
        )


def test_app_rw_cannot_delete_requests(app_conn: Conn, world: SignupWorld) -> None:
    with (
        as_user(app_conn, world.admin, world.tenant),
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        app_conn.execute("DELETE FROM signup_requests WHERE id = %s", [world.request_a])


def test_app_rw_cannot_update_request_content(app_conn: Conn, world: SignupWorld) -> None:
    with (
        as_user(app_conn, world.admin, world.tenant),
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        app_conn.execute(
            "UPDATE signup_requests SET brand_name = 'Hijacked' WHERE id = %s", [world.request_a]
        )


def _guard_refusal(info: pytest.ExceptionInfo[psycopg.Error]) -> None:
    assert info.value.sqlstate == "GRD01"
    assert "set only by staff" in str(info.value)


def test_app_rw_cannot_set_own_admin_flag(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.member, world.tenant), pytest.raises(psycopg.Error) as info:
        app_conn.execute("UPDATE users SET is_platform_admin = true WHERE id = %s", [world.member])
    _guard_refusal(info)


def test_app_rw_signup_insert_cannot_carry_admin_flag(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, None), pytest.raises(psycopg.Error) as info:
        app_conn.execute("SELECT set_config('app.login_email', 'new@apply.test', true)")
        app_conn.execute(
            "INSERT INTO users (email, hashed_password, is_platform_admin) "
            "VALUES ('new@apply.test', %s, true)",
            [HASH],
        )
    _guard_refusal(info)


def test_app_rw_admin_cannot_change_own_flag(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.admin, world.tenant), pytest.raises(psycopg.Error) as info:
        app_conn.execute("UPDATE users SET is_platform_admin = false WHERE id = %s", [world.admin])
    _guard_refusal(info)


def test_read_only_roles_never_see_phone_or_owner_name(admin_conn: Conn) -> None:
    for role in ("app_ro", "app_platform_ro"):
        for column, expected in (("phone", False), ("owner_name", False), ("brand_name", True)):
            row = admin_conn.execute(
                "SELECT has_column_privilege(%s, 'public.signup_requests', %s, 'SELECT')",
                [role, column],
            ).fetchone()
            assert row == (expected,), (role, column)


# ---- 0030 (ADR-0019 amendment): the status handle ----

HANDLE_A = bytes.fromhex("aa" * 32)
HANDLE_B = bytes.fromhex("bb" * 32)


def _issue(conn: Conn, request: UUID, digest: bytes) -> bool:
    row = conn.execute(
        "SELECT public.app_set_signup_status_handle(%s, %s)", [request, digest]
    ).fetchone()
    assert row is not None
    return bool(row[0])


def _by_hash(conn: Conn, digest: bytes) -> list[tuple[Any, ...]]:
    conn.execute("SELECT set_config('app.token_hash', %s, true)", [digest.hex()])
    return conn.execute(
        "SELECT id, brand_name FROM signup_requests WHERE status_handle_hash = %s", [digest]
    ).fetchall()


def test_handle_issued_only_for_own_pending_request(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.applicant_b):
        assert _issue(app_conn, world.request_a, HANDLE_B) is False  # someone else's request
        assert _issue(app_conn, world.request_b, b"short") is False  # not a sha256
    with as_user(app_conn, None):
        assert _issue(app_conn, world.request_a, HANDLE_A) is False  # no caller
    with as_user(app_conn, world.member, world.tenant):
        assert _issue(app_conn, world.request_a, HANDLE_A) is False
    with as_user(app_conn, world.applicant_a):
        assert _issue(app_conn, world.request_a, HANDLE_A) is True
        row = app_conn.execute(
            "SELECT status_handle_hash, status_handle_expires_at - now() "
            "FROM signup_requests WHERE id = %s",
            [world.request_a],
        ).fetchone()
        assert row is not None and bytes(row[0]) == HANDLE_A
        assert 29 * 86400 < row[1].total_seconds() <= 30 * 86400


def test_handle_reads_only_the_row_it_names(app_conn: Conn, world: SignupWorld) -> None:
    with as_user(app_conn, world.applicant_a):
        assert _issue(app_conn, world.request_a, HANDLE_A) is True
        # another browser with no user GUC: only the named row, only with its hash
        app_conn.execute("SELECT set_config('app.user_id', '', true)")
        assert _by_hash(app_conn, HANDLE_A) == [(world.request_a, "A")]
        assert _by_hash(app_conn, HANDLE_B) == []
        app_conn.execute("SELECT set_config('app.token_hash', %s, true)", [HANDLE_A.hex()])
        assert _ids(app_conn) == {world.request_a}  # never applicant B's row


def test_expired_handle_reads_nothing(app_conn: Conn, admin_conn: Conn, world: SignupWorld) -> None:
    admin_conn.execute(
        "UPDATE signup_requests SET status_handle_hash = %s, "
        "status_handle_expires_at = now() - interval '1 second' WHERE id = %s",
        [HANDLE_B, world.request_b],
    )
    try:
        with as_user(app_conn, None):
            assert _by_hash(app_conn, HANDLE_B) == []
    finally:
        admin_conn.execute(
            "UPDATE signup_requests SET status_handle_hash = NULL, "
            "status_handle_expires_at = NULL WHERE id = %s",
            [world.request_b],
        )


def test_app_rw_cannot_write_the_handle_columns(app_conn: Conn, world: SignupWorld) -> None:
    for user in (world.applicant_a, world.admin):
        with (
            as_user(app_conn, user, world.tenant if user == world.admin else None),
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            app_conn.execute(
                "UPDATE signup_requests SET status_handle_hash = %s, "
                "status_handle_expires_at = now() + interval '1 year' WHERE id = %s",
                [HANDLE_A, world.request_a],
            )


def test_read_only_roles_never_see_the_handle(admin_conn: Conn) -> None:
    for role in ("app_ro", "app_platform_ro"):
        for column in ("status_handle_hash", "status_handle_expires_at"):
            row = admin_conn.execute(
                "SELECT has_column_privilege(%s, 'public.signup_requests', %s, 'SELECT')",
                [role, column],
            ).fetchone()
            assert row == (False,), (role, column)
