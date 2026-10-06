"""S12 T4: what `build_attributes` and `seller_provenance` produce passes the database's own
`fact_map_valid` (the SQL twin behind `ck_products_attributes` and the provenance check)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from .conftest import LOCAL_HOSTS
from .products_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _valid(conn: Conn, value: object, require_value: bool) -> bool:
    row = conn.execute(
        "SELECT fact_map_valid(%s::jsonb, %s)", [json.dumps(value), require_value]
    ).fetchone()
    assert row is not None
    return bool(row[0])


@pytest.mark.parametrize(
    "inputs",
    [
        {"color": "navy blue", "material": "organic cotton"},
        {"color": True},
        {"fabric_type": 3.5},
        {"color": "नीला"},
    ],
)
def test_built_attributes_pass_fact_map_valid(
    superuser_conn: Conn, inputs: dict[str, object]
) -> None:
    from app.repositories.attribute_definitions import AttributeDef
    from app.services.product_facts import build_attributes

    defs = (
        AttributeDef("color", "attributes", ("colour", "standalone"), ()),
        AttributeDef("fabric_type", "attributes", ("style", "standalone"), ("material",)),
    )
    built = build_attributes(inputs, defs, now=datetime.now(UTC))  # type: ignore[arg-type]
    assert _valid(superuser_conn, built, True)


def test_seller_provenance_passes_fact_map_valid(superuser_conn: Conn) -> None:
    from app.services.product_facts import seller_provenance

    provenance = seller_provenance(now=datetime.now(UTC))
    assert _valid(superuser_conn, provenance, False)
    # without values it is not a valid attributes map (require_value), as intended
    assert not _valid(superuser_conn, provenance, True)
