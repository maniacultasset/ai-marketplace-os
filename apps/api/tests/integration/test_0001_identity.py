"""`tenants` and global `users` (DATABASE.md §5.1; ADR-0006 §1, ADR-0008 §2)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_USER = "INSERT INTO users (email, hashed_password) VALUES (%s, 'h')"


def test_users_email_unique_ci(db: Conn) -> None:
    db.execute(INSERT_USER, ("A@x.test",))
    expect_sqlstate(db, "23505", INSERT_USER, ("a@x.test",), constraint="uq_users_email_lower")
    db.execute(INSERT_USER, ("b@x.test",))
    stored = db.execute("SELECT email FROM users WHERE lower(email) = 'a@x.test'").fetchall()
    assert stored == [("A@x.test",)]  # the stored case is kept


@pytest.mark.parametrize(
    "email",
    [" a@x.test", "a@x.test ", "a@x.test\t", "ax.test", "a@b@c", "a@" + "x" * 253, ""],
    ids=["leading space", "trailing space", "tab", "no at", "two ats", "255 bytes", "empty"],
)
def test_0001_checks_users(db: Conn, email: str) -> None:
    expect_sqlstate(db, "23514", INSERT_USER, (email,), constraint="ck_users_email")


def test_users_email_254_bytes_ok(db: Conn) -> None:
    db.execute(INSERT_USER, ("a@" + "x" * 252,))


def test_users_credential_check(db: Conn) -> None:
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO users (email) VALUES ('nocred@x.test')",
        constraint="ck_users_credential",
    )
    db.execute("INSERT INTO users (email, erased_at) VALUES ('erased@x.test', now())")
    db.execute("INSERT INTO users (email, auth_provider) VALUES ('sso@x.test', 'google')")


def test_users_defaults(db: Conn) -> None:
    user_id = f.user(db)
    row = db.execute(
        "SELECT is_active, created_at IS NOT NULL, updated_at IS NOT NULL FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    assert row == (True, True, True)


def test_auto_publish_locked(db: Conn) -> None:
    tenant_id = f.tenant(db)
    row = db.execute("SELECT auto_publish FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    assert row == (False,)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE tenants SET auto_publish = true WHERE id = %s",
        (tenant_id,),
        constraint="ck_tenants_auto_publish",
    )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO tenants (name, slug, status, auto_publish) "
        "VALUES ('T', 'auto-pub', 'active', true)",
        constraint="ck_tenants_auto_publish",
    )


TENANT_REJECTS: list[tuple[str, dict[str, Any], str]] = [
    ("slug upper", {"slug": "A"}, "ck_tenants_slug"),
    ("slug one char", {"slug": "a"}, "ck_tenants_slug"),
    ("slug 41 chars", {"slug": "a" * 41}, "ck_tenants_slug"),
    ("slug leading dash", {"slug": "-ab"}, "ck_tenants_slug"),
    ("guidelines object", {"guidelines": {}}, "ck_tenants_guidelines"),
    ("glossary array", {"glossary": Jsonb([])}, "ck_tenants_glossary"),
    ("status Active", {"status": "Active"}, "ck_tenants_status"),
    ("trial without end", {"status": "trial"}, "ck_tenants_trial"),
    ("offboarded without at", {"status": "offboarded"}, "ck_tenants_offboarded"),
    ("offboarded_at while active", {"offboarded_at": "2026-10-01"}, "ck_tenants_offboarded"),
    ("budget NaN", {"llm_daily_budget_usd": "NaN"}, "ck_tenants_llm_daily_budget_usd"),
    ("budget negative", {"llm_daily_budget_usd": -1}, "ck_tenants_llm_daily_budget_usd"),
]


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [pytest.param(c, k, id=n) for n, c, k in TENANT_REJECTS],
)
def test_0001_checks_tenants(db: Conn, columns: dict[str, Any], constraint: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.tenant(db, **columns)
    assert info.value.diag.constraint_name == constraint


def test_0001_tenants_accepts_valid_shapes(db: Conn) -> None:
    f.tenant(db, slug="a" * 40)
    f.tenant(db, slug="0-brand")
    f.tenant(
        db,
        guidelines=Jsonb(["no claims"]),
        glossary={"kurta outfit": "kurta set"},
    )
    f.tenant(db, status="trial", trial_ends_at="2026-11-01")
    f.tenant(db, status="offboarded", offboarded_at="2026-10-01")
    f.tenant(db, status="suspended", llm_daily_budget_usd=0)


def test_tenant_slug_unique(db: Conn) -> None:
    f.tenant(db, slug="acme")
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO tenants (name, slug, status) VALUES ('Acme 2', 'acme', 'active')",
        constraint="uq_tenants_slug",
    )


def test_tenant_status_has_no_default(db: Conn) -> None:
    expect_sqlstate(db, "23502", "INSERT INTO tenants (name, slug) VALUES ('T', 'no-status')")
