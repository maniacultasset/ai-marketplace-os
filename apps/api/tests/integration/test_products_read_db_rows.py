"""S12: the read path is total over rows every DB CHECK accepts.

A row that `ProductOut`'s input patterns would refuse (written by a later importer, the AI writers
or by hand) must not turn `GET /products` into a 500 for the whole tenant, nor its own GET.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for

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


def _insert(conn: Conn, tenant: Any, sku: str, title: str, attributes: dict[str, Any]) -> Any:
    """A row every DB CHECK accepts (fact_map_valid(attributes, true) included)."""
    row = conn.execute(
        "INSERT INTO products (tenant_id, sku, title, category, attributes, provenance) "
        "VALUES (%s, %s, %s, 'kurtas', %s::jsonb, %s::jsonb) RETURNING id",
        [tenant, sku, title, json.dumps(attributes), _PROV],
    ).fetchone()
    assert row is not None
    return row[0]


# (sku, title, stored attributes, expected flat `attributes` on the wire)
_CASES = {
    # a Fact with `normalized` and no `value`: the read falls back to `normalized`
    "normalized_only": (
        "S12-NORM",
        "Kurta",
        {"color": _fact(normalized="navy")},
        {"color": "navy"},
    ),
    # a list or object value has no place in the flat scalar view (CP1): it is left out, the
    # rest of the row still reads
    "list_value": (
        "S12-LIST",
        "Kurta",
        {"color": _fact(value=["navy", "red"]), "fabric_type": _fact(value="cotton")},
        {"fabric_type": "cotton"},
    ),
    "object_value": ("S12-OBJ", "Kurta", {"color": _fact(value={"a": 1})}, {}),
    # a whitespace-only title (the DB check is only char_length 1..500)
    "blank_title": ("S12-BLANK", "   ", {}, {}),
    # a SKU with U+00A0 (the DB regex accepts it; the wire Sku pattern does not)
    "nbsp_sku": ("S12\u00a0NBSP", "Kurta", {}, {}),
}


@pytest.mark.parametrize("case", list(_CASES))
async def test_one_db_valid_row_does_not_break_the_tenant_list(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, case: str
) -> None:
    sku, title, attributes, expected = _CASES[case]
    product_id = _insert(auth_db, products_world.tenant("acme"), sku, title, attributes)
    headers = bearer_for(products_world, "operator@acme.test")

    listed = await api.get(PRODUCTS, headers=headers)
    assert listed.status_code == 200, listed.text
    (row,) = [p for p in listed.json()["data"] if p["id"] == str(product_id)]

    one = await api.get(f"{PRODUCTS}/{product_id}", headers=headers)
    assert one.status_code == 200, one.text
    assert one.json() == row
    assert (row["sku"], row["title"], row["attributes"]) == (sku, title, expected)
