"""Tester attacks on revision 0001 that hold today: privilege edges the plan's tests leave out
(the trigger under app_rw, TRUNCATE/REFERENCES/TRIGGER, filtering on a secret column, table
ownership), per-tenant natural keys, GTIN input edges and a purge with an invite chain."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import disable_0004_guard, rls_tenant
from .test_0001_catalog import TABLES_0001

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


@pytest.fixture(autouse=True)
def _0004_guards_off(request: pytest.FixtureRequest) -> None:
    """These suites test 0001's CHECKs, keys and FKs on rows the 0004 guards would refuse first (each guard
    is proven by its own test_0004_* suite); the guards are off in the rolled-back `db`
    transaction."""
    if "db" in request.fixturenames:
        db = request.getfixturevalue("db")
        disable_0004_guard(db, "variant_matrix_cells", "variant_matrix_guard")
        disable_0004_guard(db, "products", "variant_guard")
        disable_0004_guard(db, "products", "variant_matrix_complete")
        disable_0004_guard(db, "variant_matrix_cells", "variant_matrix_complete")


Conn = psycopg.Connection[tuple[Any, ...]]

APP_ROLES = ["app_rw", "app_ro", "app_platform_ro"]
# A valid EAN-13 spelled in Unicode digits that are not ASCII 0-9.
FULL_WIDTH = "".join(chr(0xFF10 + int(d)) for d in "4006381333931")
ARABIC_INDIC = "".join(chr(0x0660 + int(d)) for d in "4006381333931")


def _set_role(conn: Conn, role: str) -> None:
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


def test_updated_at_moves_under_app_rw(db: Conn) -> None:
    """`set_updated_at()` has no EXECUTE grant for app_rw; the trigger must still fire."""
    _set_role(db, "app_rw")
    tenant_id = rls_tenant(db)  # 0005: the tenant GUC comes first under RLS
    product_id = f.product(db, tenant_id)
    db.execute("SET LOCAL ROLE postgres")
    db.execute("SET LOCAL session_replication_role = replica")
    db.execute("UPDATE tenants SET updated_at = '2000-01-01' WHERE id = %s", (tenant_id,))
    db.execute("UPDATE products SET updated_at = '2000-01-01' WHERE id = %s", (product_id,))
    db.execute("SET LOCAL session_replication_role = origin")
    _set_role(db, "app_rw")
    tenant_row = db.execute(
        "UPDATE tenants SET name = 'renamed' WHERE id = %s RETURNING updated_at = now()",
        (tenant_id,),
    ).fetchone()
    product_row = db.execute(
        "UPDATE products SET description = 'd' WHERE id = %s RETURNING updated_at = now()",
        (product_id,),
    ).fetchone()
    assert tenant_row == (True,)
    assert product_row == (True,)


@pytest.mark.parametrize("role", APP_ROLES)
@pytest.mark.parametrize("table", TABLES_0001)
def test_app_roles_hold_no_truncate_references_or_trigger(db: Conn, role: str, table: str) -> None:
    for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
        row = db.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", privilege)
        ).fetchone()
        assert row == (False,), f"{role} holds {privilege} on {table}"


def test_0001_tables_owned_by_migrator(db: Conn) -> None:
    rows = db.execute(
        "SELECT relname, pg_get_userbyid(relowner) FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s) ORDER BY 1",
        (TABLES_0001,),
    ).fetchall()
    assert len(rows) == len(TABLES_0001)
    assert {owner for _, owner in rows} == {"migrator"}


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro"])
@pytest.mark.parametrize(
    ("table", "predicate"),
    [
        ("users", "hashed_password IS NULL"),
        ("invitations", "token_hash IS NOT NULL"),
        ("business_parties", "gstin LIKE '27%%'"),
        ("business_parties", "phone IS NOT NULL"),
        ("business_parties", "dedupe_key LIKE 'cc|%%'"),
    ],
)
def test_read_only_roles_cannot_filter_on_secret_columns(
    db: Conn, role: str, table: str, predicate: str
) -> None:
    """A WHERE on a column without SELECT is refused, so a count cannot leak the value."""
    _set_role(db, role)
    expect_sqlstate(
        db,
        "42501",
        sql.SQL("SELECT count(id) FROM {} WHERE {}").format(
            sql.Identifier(table), sql.SQL(predicate)
        ),
    )


def _pending_invite(conn: Conn, tenant_id: UUID, email: str) -> None:
    f.insert(
        conn,
        "invitations",
        tenant_id=tenant_id,
        email=email,
        role="operator",
        token_hash=bytes(16) + tenant_id.bytes,
        expires_at="2099-01-01",
    )


def test_natural_keys_are_per_tenant(db: Conn) -> None:
    """The same GTIN, party and pending invitee in two tenants: each key is scoped by tenant."""
    for tenant_id in (f.tenant(db), f.tenant(db)):
        f.product(
            db,
            tenant_id,
            identifier_type="ean",
            identifier_value="4006381333931",
            identifier_status="provided",
        )
        f.party(
            db,
            tenant_id,
            legal_name="Acme Textiles Pvt Ltd",
            address_line1="1 MG Road",
            postal_code="560001",
        )
        f.party(db, tenant_id, legal_name=None, roles=["consumer_care"], phone="+91 98765 43210")
        _pending_invite(db, tenant_id, "same@x.test")


@pytest.mark.parametrize(
    "value",
    [
        "4006381333931\n",
        " 4006381333931",
        "+4006381333931",
        FULL_WIDTH,
        ARABIC_INDIC,
        "40063813339.1",
        "",
    ],
)
def test_gtin_valid_rejects_lookalike_input(db: Conn, value: str) -> None:
    assert db.execute("SELECT public.gtin_valid(%s)", (value,)).fetchone() == (False,)


def test_identifier_check_rejects_lookalike_digits(db: Conn) -> None:
    tenant_id = f.tenant(db)
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.product(
            db,
            tenant_id,
            identifier_type="ean",
            identifier_value=FULL_WIDTH,
            identifier_status="provided",
        )
    assert info.value.diag.constraint_name == "ck_products_identifier_valid"


def test_purge_with_invite_chain_and_shared_parties(db: Conn) -> None:
    """One DELETE after the brand profile, with an invite chain (owner → operator → reviewer),
    invitations from a member, two styles sharing a party, and products created by members."""
    tenant_id, other = f.tenant(db), f.tenant(db)
    owner = f.member(db, tenant_id)
    operator = f.member(db, tenant_id, role="operator", invited_by_user_id=owner)
    f.member(db, tenant_id, role="reviewer", invited_by_user_id=operator)
    f.insert(
        db,
        "invitations",
        tenant_id=tenant_id,
        email="next@x.test",
        role="reviewer",
        token_hash=bytes(32),
        expires_at="2099-01-01",
        invited_by_user_id=operator,
    )
    shared = f.party(db, tenant_id, roles=["manufacturer", "packer"])
    f.brand_profile(db, tenant_id, default_manufacturer_party_id=shared)
    for _ in range(2):
        style = f.product(db, tenant_id, "style", created_by=operator, manufacturer_party_id=shared)
        colourway = f.colourway(db, tenant_id, style)
        child = f.product(
            db, tenant_id, "child", parent=style, packer_party_id=shared, created_by=owner
        )
        f.cell(
            db,
            tenant_id,
            style,
            colourway_id=colourway,
            size_key="m",
            availability="available",
            unavailable_reason=None,
            variant_product_id=child,
        )
    other_style = f.product(db, other, "style")
    f.colourway(db, other, other_style)
    db.execute("DELETE FROM brand_profiles WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    for table in TABLES_0001:
        if table in ("tenants", "users"):
            continue
        row = db.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
            (tenant_id,),
        ).fetchone()
        assert row == (0,), table
    assert db.execute(
        "SELECT count(*) FROM products WHERE tenant_id = %s", (other,)
    ).fetchone() == (1,)
