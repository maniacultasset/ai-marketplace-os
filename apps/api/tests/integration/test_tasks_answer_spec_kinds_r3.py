"""SI3 re-check round 3 (tester): F4 holds over HTTP for the AnswerSpec kinds the round 3 fix added
(DATABASE.md:1411, :1422). A float answer to a number question with float bounds, a money answer, a
composition answer and a Devanagari text answer at the limit are stored; out-of-bound floats, bad
money, a bad composition and any value for a value-less kind are 422 at `value`, write no fact and
leave the task open."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .test_tasks_answer_api import _answer, _fact, _open, _task

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

GSM: dict[str, Any] = {"kind": "number", "min": 60.5, "max": 400.5}


async def _stored(
    api: httpx.AsyncClient, world: World, conn: Conn, spec: Any, key: str, value: object
) -> None:
    task_id, product_id = await _open(world, conn, key=key, expected=spec)
    response = await _answer(api, world, task_id, {"value": value})
    assert response.status_code == 200, response.text
    assert _fact(conn, product_id, key)["value"] == value
    assert _task(conn, task_id)["status"] == "applied"


async def _refused(
    api: httpx.AsyncClient, world: World, conn: Conn, spec: Any, key: str, value: object
) -> None:
    task_id, product_id = await _open(world, conn, key=key, expected=spec)
    response = await _answer(api, world, task_id, {"value": value})
    assert response.status_code == 422, (response.status_code, _fact(conn, product_id, key))
    assert _fact(conn, product_id, key) is None
    assert _task(conn, task_id)["status"] == "open"


async def test_number_spec_takes_a_float_within_float_bounds(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _stored(api, products_world, auth_db, GSM, "gsm", 180.5)


@pytest.mark.parametrize("value", [60.4, 400.6, True, "180"])
async def test_number_spec_refuses_out_of_bound_floats_and_non_numbers(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, value: object
) -> None:
    await _refused(api, products_world, auth_db, GSM, "gsm", value)


async def test_money_spec_takes_a_decimal_string(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _stored(api, products_world, auth_db, {"kind": "money_inr"}, "fit", "1299.00")


@pytest.mark.parametrize("value", ["0.00", "12.345", "-1", 499, "1e3"])
async def test_money_spec_refuses_bad_money(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, value: object
) -> None:
    await _refused(api, products_world, auth_db, {"kind": "money_inr"}, "fit", value)


async def test_composition_spec_parses_through_the_dictionary(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    spec = {"kind": "percent_composition"}
    await _stored(api, products_world, auth_db, spec, "fiber_composition", "100% cotton")


@pytest.mark.parametrize("value", ["mostly cotton", True])
async def test_composition_spec_refuses_a_bad_composition(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, value: object
) -> None:
    spec = {"kind": "percent_composition"}
    await _refused(api, products_world, auth_db, spec, "fiber_composition", value)


@pytest.mark.parametrize("kind", ["file", "acknowledge", "reconnect", "something_new"])
async def test_a_value_less_or_unknown_kind_takes_no_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, kind: str
) -> None:
    await _refused(api, products_world, auth_db, {"kind": kind}, "fit", "regular")


async def test_text_limit_counts_characters_not_bytes(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    spec = {"kind": "text", "max_length": 256}
    await _stored(api, products_world, auth_db, spec, "fit", "र" * 256)
    await _refused(api, products_world, auth_db, spec, "fit", "र" * 257)
