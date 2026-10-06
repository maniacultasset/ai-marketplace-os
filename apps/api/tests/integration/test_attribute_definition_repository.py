"""S12 T3 (AC4): `AttributeDefinitionRepository.resolve` reads the platform dictionary by key or
input alias, enabled rows only, inside a tenant transaction as `app_rw`."""

from __future__ import annotations

import uuid

import pytest

from .conftest import LOCAL_HOSTS
from .products_support import Conn, seed_dictionary, statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

DISABLED_KEY = "s12_disabled_probe"


@pytest.fixture
def dictionary(auth_db: Conn) -> Conn:
    # dictionary rows are never deleted (0004 dictionary_guard): the disabled probe stays put
    seed_dictionary(auth_db)
    auth_db.execute(
        "INSERT INTO attribute_definitions (key, section, storage, owner_levels, value_type, "
        "input_aliases, is_critical, allowed_sources, confirm_agent_captures, enabled) VALUES "
        "(%s, 'attributes', 'attributes', ARRAY['standalone'], 'text', ARRAY['s12_alias_off'], "
        "false, ARRAY['seller'], false, false) ON CONFLICT (key) DO NOTHING",
        [DISABLED_KEY],
    )
    return auth_db


async def _resolve(names: list[str]) -> list[object]:
    from app.repositories.attribute_definitions import AttributeDefinitionRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(uuid.uuid4()) as session:
        return list(await AttributeDefinitionRepository(session).resolve(names))


async def test_resolve_by_key(dictionary: Conn) -> None:
    from app.repositories.attribute_definitions import AttributeDef

    found = await _resolve(["color"])
    assert found == [
        AttributeDef(
            key="color",
            storage="attributes",
            owner_levels=("colour", "standalone"),
            input_aliases=(),
        )
    ]


async def test_resolve_by_alias(dictionary: Conn) -> None:
    found = await _resolve(["material"])
    assert [d.key for d in found] == ["fabric_type"]  # type: ignore[attr-defined]
    assert found[0].input_aliases == ("material",)  # type: ignore[attr-defined]


async def test_resolve_excludes_disabled(dictionary: Conn) -> None:
    assert await _resolve([DISABLED_KEY]) == []
    assert await _resolve(["s12_alias_off"]) == []


async def test_resolve_unknown_empty(dictionary: Conn) -> None:
    assert await _resolve(["shade", "no_such_key"]) == []


async def test_resolve_mixed_names(dictionary: Conn) -> None:
    found = await _resolve(["color", "material", "shade"])
    assert sorted(d.key for d in found) == ["color", "fabric_type"]  # type: ignore[attr-defined]


async def test_resolve_empty_input_no_sql(dictionary: Conn) -> None:
    from app.repositories.attribute_definitions import AttributeDefinitionRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(uuid.uuid4()) as session:
        await session.connection()  # the transaction's own SET statements run first
        with statements() as seen:
            assert await AttributeDefinitionRepository(session).resolve([]) == []
    assert not [s for s in seen if "attribute_definitions" in s]


async def test_resolve_runs_as_app_rw_under_guard(dictionary: Conn) -> None:
    from app.repositories.attribute_definitions import AttributeDefinitionRepository
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import func, select

    async with unit_of_work(uuid.uuid4()) as session:
        assert await session.scalar(select(func.current_user())) == "app_rw"
        found = await AttributeDefinitionRepository(session).resolve(["color"])
    assert [d.key for d in found] == ["color"]
