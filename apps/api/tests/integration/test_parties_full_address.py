"""SI1 T8 (AC6, AC7; decision 16): the full party address, the match update only for the brand
profile route, `get`, and the brand profile `ensure`/`set_default_manufacturer`. Run in the
Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import unit_of_work
from app.repositories.parties import BrandProfileRepository, BusinessPartyRepository, PartyFields

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
STAMP = "2026-11-06T00:00:00Z"
FACT = {"source": "seller", "captured_via": "api", "updated_at": STAMP}


def _fields(**over: Any) -> PartyFields:
    base: dict[str, Any] = {
        "role": "manufacturer",
        "legal_name": f"Acme {uuid.uuid4().hex[:6]}",
        "address_line1": "12 MG Road",
        "address_line2": "Floor 2",
        "city": "Bengaluru",
        "state_code": "KA",
        "postal_code": "560001",
        "gstin": "29ABCDE1234F1Z5",
    }
    base.update(over)
    return PartyFields(**base)


def _row(conn: Conn, party_id: uuid.UUID) -> tuple[Any, ...]:
    found = conn.execute("SELECT * FROM business_parties WHERE id = %s", [party_id]).fetchone()
    assert found is not None
    return tuple(found)


def _col(conn: Conn, party_id: uuid.UUID, column: str) -> Any:
    found = conn.execute(
        f"SELECT {column} FROM business_parties WHERE id = %s", [party_id]  # noqa: S608
    ).fetchone()
    assert found is not None
    return found[0]


async def _match(tenant: uuid.UUID, fields: PartyFields, **kw: Any) -> uuid.UUID:
    async with unit_of_work(tenant) as session:
        return await BusinessPartyRepository(session, tenant).match_or_create(fields, **kw)


async def test_create_with_every_field(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    party = await _match(tenant, _fields())
    assert _col(auth_db, party, "city") == "Bengaluru"
    assert _col(auth_db, party, "state_code") == "KA"
    assert _col(auth_db, party, "gstin") == "29ABCDE1234F1Z5"
    assert _col(auth_db, party, "address_line2") == "Floor 2"


async def test_match_with_update_changes_city(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    fields = _fields()
    first = await _match(tenant, fields)
    moved = PartyFields(**{**_as_dict(fields), "city": "Mysuru"})
    second = await _match(tenant, moved, update_on_match=True)
    assert second == first
    assert _col(auth_db, first, "city") == "Mysuru"


async def test_match_without_update_keeps_row(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    fields = _fields()
    first = await _match(tenant, fields)
    before = _row(auth_db, first)
    other = PartyFields(
        **{**_as_dict(fields), "city": "Mysuru", "state_code": "TN", "gstin": "33ABCDE1234F1Z5"}
    )
    assert await _match(tenant, other) == first
    assert _row(auth_db, first) == before


async def test_match_keeps_city_when_absent(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    fields = _fields()
    first = await _match(tenant, fields)
    await _match(tenant, PartyFields(**{**_as_dict(fields), "city": None}), update_on_match=True)
    assert _col(auth_db, first, "city") == "Bengaluru"


async def test_role_appended_once(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    fields = _fields()
    party = await _match(tenant, fields)
    packer = PartyFields(**{**_as_dict(fields), "role": "packer"})
    await _match(tenant, packer)
    await _match(tenant, packer)
    assert sorted(_col(auth_db, party, "roles")) == ["manufacturer", "packer"]


async def test_get_is_tenant_scoped(products_world: World) -> None:
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    party = await _match(acme, _fields())
    async with unit_of_work(acme) as session:
        assert await BusinessPartyRepository(session, acme).get(party) is not None
    async with unit_of_work(bharat) as session:
        assert await BusinessPartyRepository(session, bharat).get(party) is None


async def test_ensure_twice_keeps_one_row(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    auth_db.execute("DELETE FROM brand_profiles WHERE tenant_id = %s", [tenant])
    async with unit_of_work(tenant) as session:
        assert await BrandProfileRepository(session, tenant).ensure() is True
    async with unit_of_work(tenant) as session:
        assert await BrandProfileRepository(session, tenant).ensure() is False
    count = auth_db.execute(
        "SELECT count(*) FROM brand_profiles WHERE tenant_id = %s", [tenant]
    ).fetchone()
    assert count == (1,)


async def test_set_default_manufacturer_and_clear(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    party = await _match(tenant, _fields())
    async with unit_of_work(tenant) as session:
        repo = BrandProfileRepository(session, tenant)
        await repo.ensure()
        assert await repo.set_default_manufacturer(party, FACT)
        assert not await repo.set_default_manufacturer(party, FACT)
    stored = auth_db.execute(
        "SELECT default_manufacturer_party_id, provenance FROM brand_profiles WHERE tenant_id = %s",
        [tenant],
    ).fetchone()
    assert stored is not None
    provenance: Any = stored[1]
    assert stored[0] == party and provenance["manufacturer"]["source"] == "seller"
    assert stored[0] == party
    async with unit_of_work(tenant) as session:
        assert await BrandProfileRepository(session, tenant).set_default_manufacturer(None, FACT)
    cleared = auth_db.execute(
        "SELECT default_manufacturer_party_id, provenance FROM brand_profiles WHERE tenant_id = %s",
        [tenant],
    ).fetchone()
    assert cleared is not None
    left: Any = cleared[1]
    assert cleared[0] is None and "manufacturer" not in left


def _as_dict(fields: PartyFields) -> dict[str, Any]:
    return {name: getattr(fields, name) for name in PartyFields.__dataclass_fields__}
