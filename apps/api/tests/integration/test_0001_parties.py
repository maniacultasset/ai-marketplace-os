"""`business_parties` and `brand_profiles` (DATABASE.md §5.6)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

BRAND_PARTY_COLUMNS = [
    "legal_entity_party_id",
    "default_manufacturer_party_id",
    "default_marketer_party_id",
    "default_packer_party_id",
    "default_importer_party_id",
    "consumer_care_party_id",
]
# Every `→ t` column here; test_0001_catalog checks the union of these lists against pg_constraint.
CROSS_TENANT_FK_COLUMNS = [f"brand_profiles.{column}" for column in BRAND_PARTY_COLUMNS]

CARE_ONLY = ["consumer_care"]


def test_business_parties_named(db: Conn) -> None:
    tenant_id = f.tenant(db)
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.party(db, tenant_id, legal_name=None, roles=["manufacturer"], phone="+91 98765 43210")
    assert info.value.diag.constraint_name == "bp_named"
    f.party(db, tenant_id, legal_name=None, roles=CARE_ONLY, phone="+91 98765 43210")
    f.party(db, tenant_id, legal_name=None, roles=CARE_ONLY, email="care@brand.test")
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        f.party(db, tenant_id, legal_name=None, roles=CARE_ONLY)


def test_business_parties_consumer_care_needs_contact(db: Conn) -> None:
    tenant_id = f.tenant(db)
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.party(db, tenant_id, roles=["seller", "consumer_care"])
    assert info.value.diag.constraint_name == "ck_business_parties_consumer_care"
    f.party(db, tenant_id, roles=["seller", "consumer_care"], email="care@brand.test")


def test_business_parties_dedupe(db: Conn) -> None:
    tenant_id = f.tenant(db)
    first = f.party(db, tenant_id, legal_name="Acme Textiles")
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        f.party(db, tenant_id, legal_name="Acme Textiles")
    assert info.value.diag.constraint_name == "bp_dedupe_uq"
    f.party(db, tenant_id, legal_name="  ACME ")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.party(db, tenant_id, legal_name="acme")
    # the same name at another address is another party; whitespace runs in the address fold
    f.party(db, tenant_id, legal_name="Acme Textiles", address_line1="12  MG Road")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.party(db, tenant_id, legal_name="acme textiles", address_line1="12 mg road")
    f.party(db, f.tenant(db), legal_name="Acme Textiles")  # another tenant
    db.execute("UPDATE business_parties SET deleted_at = now() WHERE id = %s", (first,))
    f.party(db, tenant_id, legal_name="Acme Textiles")  # a soft-deleted twin no longer counts
    # two nameless care contacts with the same phone digits collide too
    f.party(db, tenant_id, legal_name=None, roles=CARE_ONLY, phone="+91 98765-43210")
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.party(db, tenant_id, legal_name=None, roles=CARE_ONLY, phone="+919876543210")


@pytest.mark.parametrize(
    ("columns", "key"),
    [
        ({"legal_name": " Acme  ", "postal_code": "560001"}, "acme||560001|IN"),
        ({"legal_name": None, "roles": CARE_ONLY, "email": "Care@B.test"}, "cc|care@b.test|"),
        ({"legal_name": None, "roles": CARE_ONLY, "phone": "+91 98765-43210"}, "cc||919876543210"),
    ],
    ids=["named", "care-only email", "care-only phone"],
)
def test_business_parties_dedupe_key_not_null(db: Conn, columns: dict[str, Any], key: str) -> None:
    party_id = f.party(db, f.tenant(db), **columns)
    row = db.execute(
        "SELECT dedupe_key FROM business_parties WHERE id = %s", (party_id,)
    ).fetchone()
    assert row == (key,)
    nullable = db.execute(
        "SELECT attnotnull, attgenerated FROM pg_attribute "
        "WHERE attrelid = 'business_parties'::regclass AND attname = 'dedupe_key'"
    ).fetchone()
    assert nullable == (True, "s")


@pytest.mark.parametrize(
    "columns",
    [
        {"source": "ai"},
        {"source": "derived"},
        {"source": "Seller"},
        {"source": "document"},
        {"source": "document", "evidence_ref": "import_row:1"},
        {"source": "document", "evidence_ref": "url:x"},
        {"source": "pim", "evidence_ref": "image:a#b"},
    ],
    ids=["ai", "derived", "Seller", "document without ref", "prunable ref", "url ref", "fragment"],
)
def test_business_parties_source(db: Conn, columns: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        f.party(db, f.tenant(db), **columns)


def test_business_parties_source_accepts(db: Conn) -> None:
    tenant_id = f.tenant(db)
    f.party(db, tenant_id, source="document", evidence_ref="image:gst-cert")
    f.party(db, tenant_id, source="pim", evidence_ref="brand_profile:1")
    f.party(db, tenant_id, source="marketplace", evidence_ref="image:x")


PARTY_REJECTS: list[tuple[str, dict[str, Any], str]] = [
    ("roles empty", {"roles": []}, "ck_business_parties_roles"),
    (
        "roles 7",
        {"roles": ["seller", "manufacturer", "marketer", "packer", "importer", "seller", "packer"]},
        "ck_business_parties_roles",
    ),
    ("roles buyer", {"roles": ["buyer"]}, "ck_business_parties_roles"),
    ("roles NULL element", {"roles": ["seller", None]}, "ck_business_parties_roles"),
    ("legal_name empty", {"legal_name": ""}, "ck_business_parties_legal_name"),
    ("legal_name blank", {"legal_name": "   "}, "ck_business_parties_legal_name"),
    ("legal_name 301", {"legal_name": "x" * 301}, "ck_business_parties_legal_name"),
    ("address_line1 301", {"address_line1": "x" * 301}, "ck_business_parties_address_line1"),
    ("address_line2 301", {"address_line2": "x" * 301}, "ck_business_parties_address_line2"),
    ("city 101", {"city": "x" * 101}, "ck_business_parties_city"),
    ("state ka", {"state_code": "ka"}, "ck_business_parties_state_code"),
    ("IN postcode 012345", {"postal_code": "012345"}, "ck_business_parties_postal_code"),
    ("IN postcode 5 digits", {"postal_code": "56001"}, "ck_business_parties_postal_code"),
    ("country IND", {"country_code": "IND"}, "ck_business_parties_country_code"),
    ("country in", {"country_code": "in"}, "ck_business_parties_country_code"),
    ("email x", {"email": "x"}, "ck_business_parties_email"),
    ("email 255", {"email": "a@" + "x" * 253}, "ck_business_parties_email"),
    ("phone letters", {"phone": "call me"}, "ck_business_parties_phone"),
    ("phone short", {"phone": "12345"}, "ck_business_parties_phone"),
    ("gstin bad", {"gstin": "29ABCDE1234F1Z"}, "ck_business_parties_gstin"),
    ("gstin lower", {"gstin": "29abcde1234f1z5"}, "ck_business_parties_gstin"),
]


@pytest.mark.parametrize(
    ("columns", "constraint"), [pytest.param(c, k, id=n) for n, c, k in PARTY_REJECTS]
)
def test_0001_checks_parties(db: Conn, columns: dict[str, Any], constraint: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.party(db, f.tenant(db), **columns)
    assert info.value.diag.constraint_name == constraint


def test_0001_parties_accepts_valid_shapes(db: Conn) -> None:
    f.party(
        db,
        f.tenant(db),
        roles=["seller", "manufacturer", "marketer", "packer", "importer", "consumer_care"],
        address_line1="12 MG Road",
        address_line2="Near Metro",
        city="Bengaluru",
        state_code="KA",
        postal_code="560001",
        email="legal@acme.test",
        phone="+91 80 4000 1234",
        gstin="29ABCDE1234F1Z5",
    )
    f.party(db, f.tenant(db), country_code="GB", postal_code="SW1A 1AA")


BRAND_REJECTS: list[tuple[str, dict[str, Any], str]] = [
    ("21 brand names", {"brand_names": [f"b{i}" for i in range(21)]}, "brand_names"),
    ("NULL brand name", {"brand_names": ["a", None]}, "brand_names"),
    ("owner type", {"brand_owner_type": "owner"}, "brand_owner_type"),
    ("website ftp", {"website_url": "ftp://x.test"}, "website_url"),
    ("website space", {"website_url": "https://x .test"}, "website_url"),
    ("website 2001", {"website_url": "https://" + "x" * 1993}, "website_url"),
    ("description 2001", {"brand_description": "x" * 2001}, "brand_description"),
    ("locale en_in", {"content_locale": "en_in"}, "content_locale"),
    ("locale en-in", {"content_locale": "en-in"}, "content_locale"),
    ("country IND", {"default_country_of_origin": "IND"}, "default_country_of_origin"),
    ("provenance bad fact", {"provenance": {"brand_names": {"value": "x"}}}, "provenance"),
    ("provenance array", {"provenance": Jsonb([])}, "provenance"),
    ("logo s3 url", {"logo_storage_key": "s3://b/x"}, "logo_storage_key"),
    ("logo https url", {"logo_storage_key": "https://cdn/x.png"}, "logo_storage_key"),
    ("logo dotdot", {"logo_storage_key": "../x"}, "logo_storage_key"),
    ("logo inner dotdot", {"logo_storage_key": "t/../x"}, "logo_storage_key"),
    ("logo absolute", {"logo_storage_key": "/x"}, "logo_storage_key"),
    ("logo backslash", {"logo_storage_key": "a\\b"}, "logo_storage_key"),
    ("logo double slash", {"logo_storage_key": "a//b"}, "logo_storage_key"),
    ("logo control char", {"logo_storage_key": "a\x01b"}, "logo_storage_key"),
    ("logo newline", {"logo_storage_key": "a\nb"}, "logo_storage_key"),
    ("logo 1025", {"logo_storage_key": "a" * 1025}, "logo_storage_key"),
    ("logo empty", {"logo_storage_key": ""}, "logo_storage_key"),
]


@pytest.mark.parametrize(
    ("columns", "rule"), [pytest.param(c, k, id=n) for n, c, k in BRAND_REJECTS]
)
def test_0001_checks_brand_profiles(db: Conn, columns: dict[str, Any], rule: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.brand_profile(db, f.tenant(db), **columns)
    assert info.value.diag.constraint_name == f"ck_brand_profiles_{rule}"


def test_0001_brand_profiles_accepts_valid_shapes(db: Conn) -> None:
    tenant_id = f.tenant(db)
    party_id = f.party(db, tenant_id, roles=["seller"])
    f.brand_profile(
        db,
        tenant_id,
        brand_names=[f"b{i}" for i in range(20)],
        brand_owner_type="brand_owner",
        website_url="https://brand.test/about",
        brand_description="x" * 2000,
        logo_storage_key="t/logo.png",
        legal_entity_party_id=party_id,
        default_country_of_origin="IN",
        content_locale="hi-IN",
        provenance={"brand_names": f.fact()},
    )
    row = db.execute(
        "SELECT content_locale, provenance FROM brand_profiles WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    assert row is not None and row[0] == "hi-IN"
    f.brand_profile(db, f.tenant(db), logo_storage_key="a" * 1024)
    defaults = db.execute(
        "SELECT brand_names, content_locale, provenance FROM brand_profiles WHERE tenant_id = %s",
        (f.brand_profile(db, f.tenant(db)),),
    ).fetchone()
    assert defaults == ([], "en-IN", {})


def test_brand_profiles_one_per_tenant(db: Conn) -> None:
    tenant_id = f.brand_profile(db, f.tenant(db))
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO brand_profiles (tenant_id) VALUES (%s)",
        (tenant_id,),
        constraint="pk_brand_profiles",
    )


def test_brand_profiles_tenant_fk_is_no_action(db: Conn) -> None:
    tenant_id = f.brand_profile(db, f.tenant(db))
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM tenants WHERE id = %s",
        (tenant_id,),
        constraint="fk_brand_profiles_tenant_id_tenants",
    )


@pytest.mark.parametrize("column", BRAND_PARTY_COLUMNS)
def test_0001_cross_tenant_fk_brand_profiles(db: Conn, column: str) -> None:
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.brand_profile(db, tenant_a, **{column: f.party(db, tenant_a)})  # same tenant: accepted
    db.execute("DELETE FROM brand_profiles WHERE tenant_id = %s", (tenant_a,))
    foreign = f.party(db, tenant_b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
        f.brand_profile(db, tenant_a, **{column: foreign})
    assert info.value.diag.constraint_name == f"fk_brand_profiles_{column}"


def test_brand_profiles_party_delete_restricted(db: Conn) -> None:
    tenant_id = f.tenant(db)
    party_id = f.party(db, tenant_id)
    f.brand_profile(db, tenant_id, default_packer_party_id=party_id)
    expect_sqlstate(
        db,
        "23503",
        "DELETE FROM business_parties WHERE id = %s",
        (party_id,),
        constraint="fk_brand_profiles_default_packer_party_id",
    )
