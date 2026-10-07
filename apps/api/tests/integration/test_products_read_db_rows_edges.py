"""S12 tester round 2: more rows every DB CHECK accepts that the create path would never write.

Each one must read back through GET list (paged too) and GET id without a 500, and must not hide the
tenant's other rows. Falsy scalars must survive the `normalized` fallback.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, insert_products

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

_STAMP = "2026-10-06T00:00:00Z"
_PROV = json.dumps(
    {
        "title": {"source": "seller", "captured_via": "import", "updated_at": _STAMP},
        "category": {"source": "seller", "captured_via": "import", "updated_at": _STAMP},
    }
)


def _fact(**fields: Any) -> dict[str, Any]:
    return {"source": "seller", "captured_via": "import", "updated_at": _STAMP, **fields}


def _insert_raw(conn: Conn, tenant: Any, sku: str, title: str, attributes_json: str) -> Any:
    """A row a later writer may store (keys today's dictionary does not hold): written with the
    user triggers (the 0007 facts guard) off for this one transaction (`SET LOCAL`, superuser)."""
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        row = conn.execute(
            "INSERT INTO products (tenant_id, sku, title, category, attributes, provenance) "
            "VALUES (%s, %s, %s, 'kurtas', %s::jsonb, %s::jsonb) RETURNING id",
            [tenant, sku, title, attributes_json, _PROV],
        ).fetchone()
    assert row is not None
    return row[0]


_LONG = "x" * 5000  # over MAX_ATTRIBUTE_TEXT (2000); the DB caps only the whole map at 128 KiB

# (stored attributes as raw JSON text, expected flat `attributes` on the wire)
_CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "long_string_value": (json.dumps({"color": _fact(value=_LONG)}), {"color": _LONG}),
    # jsonb stores 1e400 as a 401-digit integer: it reads back as that int
    "numeric_1e400": (
        '{"color": ' + json.dumps(_fact())[:-1] + ', "value": 1e400}}',
        {"color": 10**400},
    ),
    "huge_int": (
        '{"color": ' + json.dumps(_fact())[:-1] + ', "value": 100000000000000000000000000000}}',
        {"color": 100000000000000000000000000000},
    ),
    # falsy scalars are values, not "missing": no fallback to `normalized`
    "false_value": (json.dumps({"color": _fact(value=False, normalized="x")}), {"color": False}),
    "zero_value": (json.dumps({"color": _fact(value=0, normalized="x")}), {"color": 0}),
    "empty_string_value": (json.dumps({"color": _fact(value="", normalized="x")}), {"color": ""}),
    # an explicit null value falls back to normalized; both null drops the key
    "null_value_normalized_false": (
        json.dumps({"color": _fact(value=None, normalized=False)}),
        {"color": False},
    ),
    "both_null": (json.dumps({"color": _fact(value=None, normalized=None)}), {}),
    "normalized_list": (json.dumps({"color": _fact(normalized=["a"])}), {}),
    # more keys than the create cap (200)
    "many_keys": (
        json.dumps({f"k{i:03d}": _fact(value=i) for i in range(250)}),
        {f"k{i:03d}": i for i in range(250)},
    ),
}


@pytest.mark.parametrize("case", list(_CASES))
async def test_db_valid_edge_row_reads_back(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, case: str
) -> None:
    stored, expected = _CASES[case]
    tenant = products_world.tenant("acme")
    insert_products(auth_db, tenant, 2, prefix="S12-OK")
    product_id = _insert_raw(auth_db, tenant, f"S12-{case}", "Kurta", stored)
    headers = bearer_for(products_world, "operator@acme.test")

    listed = await api.get(PRODUCTS, headers=headers)
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert len(data) == 3
    (row,) = [p for p in data if p["id"] == str(product_id)]
    assert row["attributes"] == expected

    one = await api.get(f"{PRODUCTS}/{product_id}", headers=headers)
    assert one.status_code == 200, one.text
    assert one.json() == row


async def test_paging_walks_through_every_hostile_row(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """All the hostile rows at once, walked one per page: every page is 200 and every id shows
    exactly once."""
    tenant = products_world.tenant("acme")
    ids = {str(i) for i in insert_products(auth_db, tenant, 2, prefix="S12-OK")}
    for case, (stored, _expected) in _CASES.items():
        ids.add(str(_insert_raw(auth_db, tenant, f"S12-{case}", "Kurta", stored)))
    ids.add(str(_insert_raw(auth_db, tenant, "S12\u00a0X", "  ", "{}")))
    headers = bearer_for(products_world, "operator@acme.test")

    seen: list[str] = []
    params: dict[str, Any] = {"limit": 1}
    for _ in range(len(ids) + 2):
        page = await api.get(PRODUCTS, headers=headers, params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        seen += [p["id"] for p in body["data"]]
        if not body["page"]["has_more"]:
            break
        params = {"limit": 1, "cursor": body["page"]["next_cursor"]}
    assert sorted(seen) == sorted(ids)


async def test_hostile_rows_stay_in_their_tenant(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """A DB-valid hostile row in acme is still invisible to another tenant, by list and by id."""
    stored, _ = _CASES["long_string_value"]
    product_id = _insert_raw(auth_db, products_world.tenant("acme"), "S12-X", "  ", stored)
    other = bearer_for(products_world, "operator@bharat.test")

    listed = await api.get(PRODUCTS, headers=other)
    assert listed.status_code == 200, listed.text
    assert str(product_id) not in {p["id"] for p in listed.json()["data"]}
    one = await api.get(f"{PRODUCTS}/{product_id}", headers=other)
    assert one.status_code == 404, one.text
