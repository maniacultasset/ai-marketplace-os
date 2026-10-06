"""The auth test world (CODE-PLAN S10 C4-C14): tenants, users and memberships made through the
admin connection, so the app under test (as `app_rw`, under FORCE RLS) only ever reads them.

Every user gets the one test password, hashed once per session with the real argon2id hasher.
`tokens_for()` signs JWTs with the CI secret for the probe and route tests; login tests get theirs
from `POST /api/v1/auth/login`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]
PASSWORD = "test-password-0123"
PREFIX = "/api/v1"


@cache
def password_hash() -> str:
    from app.core.passwords import hash_password

    return hash_password(PASSWORD)


def add_tenant(
    conn: Conn, slug: str, *, status: str = "active", name: str | None = None
) -> uuid.UUID:
    extra: dict[str, Any] = {}
    if status == "trial":
        extra["trial_ends_at"] = datetime.now(UTC) + timedelta(days=14)
    if status == "offboarded":
        extra["offboarded_at"] = datetime.now(UTC)
        extra["purge_after"] = datetime.now(UTC) + timedelta(days=30)
    columns = {"name": name or slug.title(), "slug": slug, "status": status, **extra}
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    row = conn.execute(
        f"INSERT INTO tenants ({names}) VALUES ({marks}) RETURNING id",  # noqa: S608
        list(columns.values()),
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def add_user(
    conn: Conn, email: str, *, active: bool = True, display_name: str | None = None
) -> uuid.UUID:
    row = conn.execute(
        "INSERT INTO users (email, hashed_password, is_active, display_name) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        [email, password_hash(), active, display_name],
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def add_member(
    conn: Conn,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    *,
    active: bool = True,
    last_used_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role, is_active, deactivated_at, "
        "last_used_at) VALUES (%s, %s, %s, %s, %s, %s)",
        [tenant_id, user_id, role, active, None if active else datetime.now(UTC), last_used_at],
    )


@dataclass
class World:
    """Two tenants, `acme` and `bharat`, with one user per role each (`<role>@<slug>.test`)."""

    conn: Conn
    tenants: dict[str, uuid.UUID] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)

    def tenant(self, slug: str) -> uuid.UUID:
        return self.tenants[slug]

    def user(self, email: str) -> uuid.UUID:
        return self.users[email]


ROLES = ("owner", "operator", "reviewer", "admin")


def build_world(conn: Conn) -> World:
    world = World(conn)
    for slug in ("acme", "bharat"):
        world.tenants[slug] = add_tenant(conn, slug)
        for role in ROLES:
            email = f"{role}@{slug}.test"
            world.users[email] = add_user(conn, email, display_name=role.title())
            add_member(conn, world.tenants[slug], world.users[email], role)
    return world


def tokens_for(
    world: World, email: str, slug: str, role: str | None = None, *, typ: str = "access"
) -> str:
    """A signed token for `email` in `slug`; `role` defaults to the seeded one."""
    from app.core.security import encode, issue_claims

    claims = issue_claims(
        sub=world.user(email),
        tid=world.tenant(slug),
        role=(role or email.split("@", 1)[0]),  # type: ignore[arg-type]
        email=email,
        typ=typ,  # type: ignore[arg-type]
    )
    return encode(claims)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def audit_rows(conn: Conn, action: str) -> list[tuple[Any, ...]]:
    """`audit_log` rows for `action`, read on the admin connection (a separate connection from the
    app's, so a row is there only if the app committed it)."""
    return conn.execute(
        "SELECT tenant_id, actor_type, actor_user_id, actor_email, entity_type, entity_id, "
        "before, after, request_id, reason FROM audit_log WHERE action = %s ORDER BY occurred_at",
        [action],
    ).fetchall()
