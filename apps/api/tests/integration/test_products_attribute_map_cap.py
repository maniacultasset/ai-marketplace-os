"""Audit DB-1: the stored attribute map must fit `fact_map_valid`'s 128 KiB cap. The schema caps
each value in characters; the database counts UTF-8 bytes of the whole fact envelope. The
service measures the map the way PostgreSQL prints it and refuses an over-size one with a 422
`VALIDATION_FAILED` on `attributes`, never the CHECK's 500."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef
from app.services.product_facts import FACT_MAP_MAX_BYTES, build_attributes, jsonb_text_bytes
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from psycopg.types.json import Jsonb

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bearer_for, counts, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PgConn = psycopg.Connection[tuple[Any, ...]]

# 20 `storage = attributes` text keys a standalone product may carry (the dictionary seed)
KEYS = [
    "fabric_type", "wash_care", "iron_care", "color", "secondary_color", "body_type",
    "construction", "weave_finish", "transparency", "stretch", "fit", "pattern", "silhouette",
    "neckline", "collar_type", "sleeve_type", "sleeve_length", "cuff_type", "garment_length",
    "rise",
]  # fmt: skip
EMOJI = "\U0001f600" * 2000  # the schema's 2000 characters, 8000 UTF-8 bytes
NOW = datetime(2026, 10, 6, 9, 30, 15, 123456, tzinfo=UTC)


def _octets(db: PgConn, value: Any) -> int:
    row = db.execute("SELECT octet_length(%s::jsonb::text)", [Jsonb(value)]).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"a": "plain"},
        {"a": "हिन्दी कुर्ता", "b": "\U0001f600 emoji", "c": 'quote " and \\ slash /'},
        {"a": "tab\tnew\nline\rcr\b\f\x01\x1f\x7f"},
        {"a": 1, "b": -42, "c": 10**30, "d": True, "e": False, "f": None},
        {"a": 0.1, "b": 1e20, "c": 1e-7, "d": 1.5e300, "e": -2.5e-300, "f": 123.456},
        {"a": {"value": "x", "source": "seller"}, "b": [1, "two", {"c": []}]},
    ],
)
def test_jsonb_text_bytes_matches_postgres(db: PgConn, value: Any) -> None:
    assert jsonb_text_bytes(value) == _octets(db, value)


_SCALARS = (
    st.text(st.characters(exclude_categories=["Cs"], exclude_characters="\x00"), max_size=40)
    | st.integers(min_value=-(10**20), max_value=10**20)
    | st.floats(allow_nan=False, allow_infinity=False).filter(lambda f: f != 0.0)
    | st.booleans()
)


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.dictionaries(st.from_regex(r"[a-z][a-z0-9_]{1,10}", fullmatch=True), _SCALARS))
def test_jsonb_text_bytes_property(db: PgConn, value: dict[str, Any]) -> None:
    assert jsonb_text_bytes(value) == _octets(db, value)


def _defs() -> list[AttributeDef]:
    return [AttributeDef(k, "attributes", ("standalone",), ()) for k in KEYS]


def test_build_attributes_refuses_one_byte_over_the_cap(db: PgConn) -> None:
    base = build_attributes(dict.fromkeys(KEYS, ""), _defs(), now=NOW)
    room = FACT_MAP_MAX_BYTES - jsonb_text_bytes(base)
    share, extra = divmod(room, len(KEYS))
    fits = {k: "x" * (share + (extra if i == 0 else 0)) for i, k in enumerate(KEYS)}
    built = build_attributes(fits, _defs(), now=NOW)
    assert _octets(db, built) == FACT_MAP_MAX_BYTES
    assert db.execute("SELECT fact_map_valid(%s, true)", [Jsonb(built)]).fetchone() == (True,)
    over = {**fits, KEYS[0]: fits[KEYS[0]] + "x"}
    with pytest.raises(InputError) as caught:
        build_attributes(over, _defs(), now=NOW)
    assert (caught.value.code, caught.value.field) == (ErrorCode.VALIDATION_FAILED, "attributes")


async def test_products_create_over_size_attribute_map_is_422(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    body = site_body("DB1-BIG", attributes=dict.fromkeys(KEYS, EMOJI))
    response = await api.post(
        PRODUCTS, json=body, headers=bearer_for(products_world, "operator@acme.test")
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert error["details"] == [{"loc": ["body", "attributes"], "msg": "Invalid value."}]
    assert EMOJI[:10] not in response.text
    assert counts(auth_db) == (0, 0)


async def test_products_create_multibyte_map_under_the_cap_is_201(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    body = site_body("DB1-FITS", attributes=dict.fromkeys(KEYS[:15], EMOJI))
    response = await api.post(
        PRODUCTS, json=body, headers=bearer_for(products_world, "operator@acme.test")
    )
    assert response.status_code == 201, response.text
