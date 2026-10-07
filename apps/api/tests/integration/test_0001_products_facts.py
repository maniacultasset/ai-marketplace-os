"""`products` fact columns: commercial, compliance, parties, identifier, dimensions, `attributes`
and `provenance` (DATABASE.md §5.6 "`products` fact columns")."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from . import factories as f

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

PRODUCT_PARTY_COLUMNS = [
    "manufacturer_party_id",
    "marketer_party_id",
    "packer_party_id",
    "importer_party_id",
    "consumer_care_party_id",
]
# Every `→ t` column here; test_0001_catalog checks the union of these lists against pg_constraint.
CROSS_TENANT_FK_COLUMNS = [f"products.{column}" for column in PRODUCT_PARTY_COLUMNS]

EAN13 = "4006381333931"
DIMS: dict[str, Any] = {"package_length_cm": 30, "package_breadth_cm": 20, "package_height_cm": 5}
IDENT: dict[str, Any] = {
    "identifier_type": "ean",
    "identifier_value": EAN13,
    "identifier_status": "provided",
}


def _fails(conn: Conn, error: type[psycopg.Error], constraint: str, **kwargs: Any) -> None:
    with pytest.raises(error) as info, conn.transaction():
        f.product(conn, **kwargs)
    assert info.value.diag.constraint_name == constraint


def test_products_identifier_uq(db: Conn) -> None:
    tenant_id = f.tenant(db)
    first = f.product(db, tenant_id, **IDENT)
    _fails(
        db,
        psycopg.errors.UniqueViolation,
        "products_identifier_uq",
        tenant_id=tenant_id,
        **{**IDENT, "identifier_type": "gtin", "identifier_value": "0" + EAN13},
    )
    f.product(db, f.tenant(db), **IDENT)  # another tenant
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (first,))
    f.product(db, tenant_id, **{**IDENT, "identifier_value": "0" + EAN13})


FACT_REJECTS: list[tuple[str, dict[str, Any], str]] = [
    ("mrp 0", {"mrp": 0}, "ck_products_mrp"),
    ("mrp negative", {"mrp": -1}, "ck_products_mrp"),
    ("mrp NaN", {"mrp": "NaN"}, "ck_products_mrp"),
    ("selling 0", {"selling_price": 0}, "ck_products_selling_price"),
    ("selling NaN", {"selling_price": "NaN"}, "ck_products_selling_price"),
    ("selling above mrp", {"mrp": 100, "selling_price": 101}, "ck_price_le_mrp"),
    ("currency USD", {"currency": "USD"}, "ck_products_currency"),
    ("tax -1", {"tax_rate": -1}, "ck_products_tax_rate"),
    ("tax 100.01", {"tax_rate": 100.01}, "ck_products_tax_rate"),
    ("tax NaN", {"tax_rate": "NaN"}, "ck_products_tax_rate"),
    ("hsn 5 digits", {"hsn_code": "12345"}, "ck_products_hsn_code"),
    ("hsn letters", {"hsn_code": "61O9"}, "ck_products_hsn_code"),
    ("origin in", {"country_of_origin": "in"}, "ck_products_country_of_origin"),
    ("origin IND", {"country_of_origin": "IND"}, "ck_products_country_of_origin"),
    ("generic_name empty", {"generic_name": ""}, "ck_products_generic_name"),
    ("generic_name 101", {"generic_name": "x" * 101}, "ck_products_generic_name"),
    ("net_quantity 0", {"net_quantity": 0, "net_quantity_unit": "g"}, "ck_products_net_quantity"),
    (
        "net_quantity NaN",
        {"net_quantity": "NaN", "net_quantity_unit": "g"},
        "ck_products_net_quantity",
    ),
    ("unit lb", {"net_quantity": 1, "net_quantity_unit": "lb"}, "ck_products_net_quantity_unit"),
    ("unit without value", {"net_quantity_unit": "g"}, "ck_products_net_quantity_pair"),
    ("value without unit", {"net_quantity": 1}, "ck_products_net_quantity_pair"),
    (
        "dims 2 of 3",
        {"package_length_cm": 30, "package_breadth_cm": 20},
        "ck_products_dims_all_or_none",
    ),
    ("length 0", {**DIMS, "package_length_cm": 0}, "ck_products_package_length_cm"),
    ("breadth NaN", {**DIMS, "package_breadth_cm": "NaN"}, "ck_products_package_breadth_cm"),
    ("height negative", {**DIMS, "package_height_cm": -1}, "ck_products_package_height_cm"),
    ("package weight 0", {"package_weight_g": 0}, "ck_products_package_weight_g"),
    ("product weight 0", {"product_weight_g": 0}, "ck_products_product_weight_g"),
    ("identifier type isbn", {**IDENT, "identifier_type": "isbn"}, "ck_products_identifier_type"),
    (
        "identifier status unknown",
        {"identifier_status": "unknown"},
        "ck_products_identifier_status",
    ),
    (
        "type without value",
        {"identifier_type": "ean", "identifier_status": "exempt"},
        "ck_products_identifier_pair",
    ),
    (
        "value with exempt",
        {**IDENT, "identifier_status": "exempt"},
        "ck_products_identifier_provided",
    ),
    (
        "provided without value",
        {"identifier_status": "provided"},
        "ck_products_identifier_provided",
    ),
    (
        "bad check digit",
        {**IDENT, "identifier_value": "4006381333932"},
        "ck_products_identifier_valid",
    ),
    (
        "identifier on a style",
        {**IDENT, "kind": "style"},
        "ck_products_identifier_not_on_style",
    ),
    (
        "mrp without provenance.mrp",
        {"mrp": 100, "provenance": {"title": f.fact(), "category": f.fact()}},
        "ck_products_typed_facts_sourced",
    ),
    (
        "missing provenance.title",
        {"provenance": {"category": f.fact()}},
        "ck_products_typed_facts_sourced",
    ),
    (
        "dims without provenance.package_dimensions",
        {**DIMS, "provenance": {"title": f.fact(), "category": f.fact()}},
        "ck_products_typed_facts_sourced",
    ),
    (
        "identifier without provenance.identifier",
        {"identifier_status": "exempt", "provenance": {"title": f.fact(), "category": f.fact()}},
        "ck_products_typed_facts_sourced",
    ),
    (
        "gender without provenance.gender",
        {"gender": "men", "provenance": {"title": f.fact(), "category": f.fact()}},
        "ck_products_typed_facts_sourced",
    ),
    ("gender male", {"gender": "male"}, "ck_products_gender"),
    (
        "ai attribute without confidence",
        {"attributes": {"fabric": f.fact("ai", confidence=None)}},
        "ck_products_attributes",
    ),
    (
        "attribute without value",
        {"attributes": {"fabric": f.fact(value=None)}},
        "ck_products_attributes",
    ),
    (
        "provenance bad fact",
        {"provenance": {"title": f.fact(), "category": f.fact(source="model")}},
        "ck_products_provenance",
    ),
]


@pytest.mark.parametrize(
    ("columns", "constraint"), [pytest.param(c, k, id=n) for n, c, k in FACT_REJECTS]
)
def test_0001_checks_products(db: Conn, columns: dict[str, Any], constraint: str) -> None:
    f.facts_guard_off(db)
    _fails(db, psycopg.errors.CheckViolation, constraint, tenant_id=f.tenant(db), **columns)


def test_0001_products_facts_accepts(db: Conn) -> None:
    f.facts_guard_off(db)
    tenant_id = f.tenant(db)
    party = f.party(db, tenant_id)
    product_id = f.product(
        db,
        tenant_id,
        mrp=999,
        selling_price=999,
        tax_rate=12,
        hsn_code="61091000",
        country_of_origin="IN",
        generic_name="T-shirt",
        net_quantity=1,
        net_quantity_unit="N",
        manufacturer_party_id=party,
        marketer_party_id=party,
        packer_party_id=party,
        importer_party_id=party,
        consumer_care_party_id=party,
        package_weight_g=250,
        product_weight_g=180,
        attributes={"fabric": f.fact(normalized="cotton")},
        **DIMS,
        **IDENT,
    )
    row = db.execute("SELECT currency FROM products WHERE id = %s", (product_id,)).fetchone()
    assert row == ("INR",)
    f.product(db, tenant_id, tax_rate=0)
    f.product(db, tenant_id, tax_rate=100, hsn_code="6109")
    f.product(db, tenant_id, identifier_status="exempt")
    f.product(db, tenant_id, "style", identifier_status="not_applicable")


@pytest.mark.parametrize("column", PRODUCT_PARTY_COLUMNS)
def test_0001_cross_tenant_fk_products_parties(db: Conn, column: str) -> None:
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    same: dict[str, Any] = {column: f.party(db, tenant_a)}
    f.product(db, tenant_a, **same)  # same tenant: accepted
    _fails(
        db,
        psycopg.errors.ForeignKeyViolation,
        f"fk_products_{column}",
        tenant_id=tenant_a,
        **{column: f.party(db, tenant_b)},
    )


def test_products_party_delete_restricted(db: Conn) -> None:
    tenant_id = f.tenant(db)
    party = f.party(db, tenant_id)
    f.product(db, tenant_id, packer_party_id=party)
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
        db.execute("DELETE FROM business_parties WHERE id = %s", (party,))
    assert info.value.diag.constraint_name == "fk_products_packer_party_id"
