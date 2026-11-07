"""SI3 fix round 3 (F4): `tasks_answer` checks every AnswerSpec kind (DATABASE.md:1411, :1422), not
only enum, multi_enum and number. The shapes are the ones the SI4 issue-question lane opens
(`{kind: 'text', max_length: 256}`, `{kind: 'boolean'}`). A refused answer writes no fact and
leaves the task open."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .test_tasks_answer_api import _answer, _fact, _open, _task

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

TEXT_256: dict[str, Any] = {"kind": "text", "max_length": 256}
BOOLEAN: dict[str, Any] = {"kind": "boolean"}


async def _refused(
    api: httpx.AsyncClient, world: World, conn: Conn, spec: Any, key: str, value: object
) -> None:
    task_id, product_id = await _open(world, conn, key=key, expected=spec)
    response = await _answer(api, world, task_id, {"value": value})
    assert response.status_code == 422, (response.status_code, _fact(conn, product_id, key))
    assert _fact(conn, product_id, key) is None
    assert _task(conn, task_id)["status"] == "open"


async def test_text_spec_max_length_is_enforced(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _refused(api, products_world, auth_db, TEXT_256, "fit", "a" * 1500)


async def test_text_spec_refuses_a_non_text_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _refused(api, products_world, auth_db, TEXT_256, "fit", True)


async def test_boolean_spec_refuses_a_string(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    await _refused(api, products_world, auth_db, BOOLEAN, "fit", "banana")


async def test_text_spec_within_max_length_is_stored(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db, key="fit", expected=TEXT_256)
    response = await _answer(api, products_world, task_id, {"value": "a" * 256})
    assert response.status_code == 200, response.text
    assert _fact(auth_db, product_id, "fit")["value"] == "a" * 256
    assert _task(auth_db, task_id)["status"] == "applied"
