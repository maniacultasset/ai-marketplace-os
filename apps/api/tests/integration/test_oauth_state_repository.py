"""S15 T9 (AC2, AC4): the `oauth_states` repository and the callback's identity consume, as
`app_rw` under FORCE RLS. A state row is consumed once by hash, only for its marketplace, never as a
pending row; a pending row is consumed once by the user who started the flow, in its tenant."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from .auth_support import World, build_world
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
Conn = psycopg.Connection[tuple[Any, ...]]


def _h(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _sealed() -> Any:
    from app.core.crypto import SealedTokens

    return SealedTokens(blob=b"MKT1" + secrets.token_bytes(80), key_version="local-1")


def _grant(seller: str = "A1SELLER") -> Any:
    from app.adapters.oauth import TokenGrant
    from app.core.crypto import TokenPair

    return TokenGrant(
        tokens=TokenPair(access_token=SecretStr("a"), refresh_token=SecretStr("r")),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("s1", "s2"),
        seller_id=seller,
        region="IN",
    )


async def _state(
    world: World, slug: str, label: str, *, marketplace: str = "amazon", ttl: int = 600
) -> None:
    from app.repositories.oauth_states import OAuthStateRepository
    from app.repositories.transactions import unit_of_work

    tenant = world.tenant(slug)
    async with unit_of_work(tenant) as session:
        await OAuthStateRepository(session, tenant).insert_state(
            state_hash=_h(label),
            user_id=world.user(f"owner@{slug}.test"),
            marketplace=marketplace,
            code_verifier="v" * 64 if marketplace == "flipkart" else None,
            ttl_s=ttl,
        )


async def _pending(
    world: World, slug: str, label: str, *, email: str | None = None, ttl: int = 600
) -> None:
    from app.repositories.oauth_states import OAuthStateRepository
    from app.repositories.transactions import unit_of_work

    tenant = world.tenant(slug)
    async with unit_of_work(tenant) as session:
        await OAuthStateRepository(session, tenant).insert_pending(
            handle_hash=_h(label),
            user_id=world.user(email or f"owner@{slug}.test"),
            marketplace="amazon",
            sealed=_sealed(),
            grant=_grant(),
            ttl_s=ttl,
        )


async def _consume_state(label: str, marketplace: str = "amazon") -> Any:
    from app.repositories.identity import consume_oauth_state
    from app.repositories.transactions import identity_session

    async with identity_session() as session:
        return await consume_oauth_state(session, state_hash=_h(label), marketplace=marketplace)


async def _consume_pending(world: World, slug: str, label: str, email: str | None = None) -> Any:
    from app.repositories.oauth_states import OAuthStateRepository
    from app.repositories.transactions import unit_of_work

    tenant = world.tenant(slug)
    async with unit_of_work(tenant) as session:
        return await OAuthStateRepository(session, tenant).consume_pending(
            handle_hash=_h(label), user_id=world.user(email or f"owner@{slug}.test")
        )


def _count(conn: Conn, label: str) -> int:
    row = conn.execute("SELECT count(*) FROM oauth_states WHERE state_hash = %s", [_h(label)])
    return int(row.fetchone()[0])  # type: ignore[index]


def _expire(conn: Conn, label: str) -> None:
    conn.execute(
        "UPDATE oauth_states SET expires_at = now() - interval '1 second' WHERE state_hash = %s",
        [_h(label)],
    )


async def test_consume_state_once(auth_db: Conn, world: World) -> None:
    await _state(world, "acme", "s-once", marketplace="flipkart")
    first = await _consume_state("s-once", "flipkart")
    assert first is not None
    assert (first.tenant_id, first.user_id) == (world.tenant("acme"), world.user("owner@acme.test"))
    assert first.marketplace == "flipkart" and first.code_verifier == "v" * 64
    assert await _consume_state("s-once", "flipkart") is None
    assert _count(auth_db, "s-once") == 0


async def test_consume_state_expired_none(auth_db: Conn, world: World) -> None:
    await _state(world, "acme", "s-exp")
    _expire(auth_db, "s-exp")
    assert await _consume_state("s-exp") is None
    assert _count(auth_db, "s-exp") == 1


async def test_consume_state_unknown_none(world: World) -> None:
    assert await _consume_state("s-never-issued") is None


async def test_consume_state_marketplace_mismatch_none_and_row_kept(
    auth_db: Conn, world: World
) -> None:
    await _state(world, "acme", "s-mkt", marketplace="amazon")
    assert await _consume_state("s-mkt", "flipkart") is None
    assert _count(auth_db, "s-mkt") == 1
    assert await _consume_state("s-mkt", "amazon") is not None


async def test_consume_state_pending_row_not_consumable_as_state(
    auth_db: Conn, world: World
) -> None:
    await _pending(world, "acme", "p-as-state")
    assert await _consume_state("p-as-state") is None
    assert _count(auth_db, "p-as-state") == 1


async def test_consume_pending_once(auth_db: Conn, world: World) -> None:
    await _pending(world, "acme", "p-once")
    row = await _consume_pending(world, "acme", "p-once")
    assert row is not None
    assert (row.seller_id, row.kms_key_version, row.region) == ("A1SELLER", "local-1", "IN")
    assert row.scopes == ("s1", "s2") and row.token_enc.startswith(b"MKT1")
    assert row.token_expires_at is not None
    assert await _consume_pending(world, "acme", "p-once") is None
    assert _count(auth_db, "p-once") == 0


async def test_consume_pending_other_user_none_and_row_kept(auth_db: Conn, world: World) -> None:
    await _pending(world, "acme", "p-user")
    assert await _consume_pending(world, "acme", "p-user", email="admin@acme.test") is None
    assert _count(auth_db, "p-user") == 1


async def test_consume_pending_other_tenant_none(auth_db: Conn, world: World) -> None:
    await _pending(world, "acme", "p-tenant")
    assert await _consume_pending(world, "bharat", "p-tenant") is None
    assert _count(auth_db, "p-tenant") == 1


async def test_consume_pending_state_row_not_consumable_as_pending(
    auth_db: Conn, world: World
) -> None:
    await _state(world, "acme", "s-as-pending")
    assert await _consume_pending(world, "acme", "s-as-pending") is None
    assert _count(auth_db, "s-as-pending") == 1


async def test_consume_pending_expired_none(auth_db: Conn, world: World) -> None:
    await _pending(world, "acme", "p-exp")
    _expire(auth_db, "p-exp")
    assert await _consume_pending(world, "acme", "p-exp") is None


async def test_consume_state_two_concurrent_one_wins(world: World) -> None:
    await _state(world, "acme", "s-race")
    results = await asyncio.gather(*(_consume_state("s-race") for _ in range(4)))
    assert sum(r is not None for r in results) == 1


async def test_prune_expired_both_phases_only_expired(auth_db: Conn, world: World) -> None:
    from app.repositories.oauth_states import OAuthStateRepository
    from app.repositories.transactions import unit_of_work

    await _state(world, "acme", "s-live")
    await _state(world, "acme", "s-old")
    await _pending(world, "acme", "p-live")
    await _pending(world, "acme", "p-old")
    await _state(world, "bharat", "s-bharat-old")
    for label in ("s-old", "p-old", "s-bharat-old"):
        _expire(auth_db, label)
    tenant = world.tenant("acme")
    async with unit_of_work(tenant) as session:
        pruned = await OAuthStateRepository(session, tenant).prune_expired()
    assert pruned == 2
    left = {bytes(r[0]) for r in auth_db.execute("SELECT state_hash FROM oauth_states").fetchall()}
    assert left == {_h("s-live"), _h("p-live"), _h("s-bharat-old")}


async def test_insert_state_uses_db_clock(auth_db: Conn, world: World) -> None:
    await _state(world, "acme", "s-clock", ttl=600)
    row = auth_db.execute(
        "SELECT expires_at - created_at, code_verifier FROM oauth_states WHERE state_hash = %s",
        [_h("s-clock")],
    ).fetchone()
    assert row is not None
    assert row[0] == timedelta(seconds=600)
    assert row[1] is None  # Amazon: no PKCE verifier


@pytest.mark.parametrize("ttl", [0, -1, 86_401])
async def test_insert_state_refuses_bad_ttl(world: World, ttl: int) -> None:
    with pytest.raises(ValueError):
        await _state(world, "acme", f"s-ttl-{ttl}", ttl=ttl)


async def test_insert_refuses_bad_hash(world: World) -> None:
    from app.repositories.oauth_states import OAuthStateRepository
    from app.repositories.transactions import unit_of_work

    tenant = world.tenant("acme")
    with pytest.raises(ValueError):
        async with unit_of_work(tenant) as session:
            await OAuthStateRepository(session, tenant).insert_state(
                state_hash=b"short",
                user_id=uuid.uuid4(),
                marketplace="amazon",
                code_verifier=None,
                ttl_s=600,
            )
