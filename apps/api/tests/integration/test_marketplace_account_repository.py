"""S15 T10 (AC3, AC8-10): the account and reconnect-task repositories, as `app_rw` under FORCE
RLS: one live account per tenant and marketplace, one live seller across tenants (T31), revoke
deletes the tokens, compare-and-set refresh writes, the refresh lock, the reconnect task."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from .auth_support import World, build_world
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _sealed(tag: bytes = b"") -> Any:
    from app.core.crypto import SealedTokens

    return SealedTokens(blob=b"MKT1" + tag + secrets.token_bytes(60), key_version="local-1")


async def _insert(
    world: World, slug: str, seller: str = "A1SELLER", marketplace: str = "amazon"
) -> Any:
    from app.repositories.uow import tenant_uow

    tenant = world.tenant(slug)
    async with tenant_uow(tenant) as uow:
        return await uow.marketplace_accounts.insert_live(
            marketplace=marketplace,
            seller_id=seller,
            region="IN",
            scopes=("s1",),
            sealed=_sealed(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            connected_by=world.user(f"owner@{slug}.test"),
        )


async def _revoke(world: World, slug: str, account_id: uuid.UUID) -> None:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant(slug)) as uow:
        await uow.marketplace_accounts.revoke(account_id)


def _row(conn: Conn, account_id: uuid.UUID) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM marketplace_accounts WHERE id = %s", [account_id])
    names = [d.name for d in cur.description or []]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_insert_live_then_conflict_none(world: World) -> None:
    first = await _insert(world, "acme")
    assert first is not None and first.status == "active"
    assert await _insert(world, "acme", seller="A2OTHER") is None


async def test_insert_live_after_revoke_ok(world: World) -> None:
    first = await _insert(world, "acme")
    await _revoke(world, "acme", first.id)
    second = await _insert(world, "acme")
    assert second is not None and second.id != first.id


async def test_insert_live_seller_live_in_other_tenant_raises(world: World) -> None:
    from app.db.models.catalog_assets import UQ_MA_SELLER_LIVE
    from app.repositories.marketplace_accounts import SellerLiveElsewhere

    acme = await _insert(world, "acme", seller="A1SHARED")
    with pytest.raises(SellerLiveElsewhere) as info:
        await _insert(world, "bharat", seller="A1SHARED")
    assert info.value.constraint == UQ_MA_SELLER_LIVE
    assert info.value.code.value == "MARKETPLACE_ACCOUNT_CONFLICT"
    rendered = str(info.value) + repr(info.value)
    for value in (str(acme.id), str(world.tenant("acme")), "acme", "A1SHARED"):
        assert value not in rendered
    assert info.value.__cause__ is None and info.value.__context__ is None


async def test_insert_live_seller_needs_reconnect_in_other_tenant_raises(
    auth_db: Conn, world: World
) -> None:
    from app.repositories.marketplace_accounts import SellerLiveElsewhere

    acme = await _insert(world, "acme", seller="A1SHARED")
    auth_db.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', "
        "needs_reconnect_since = now() WHERE id = %s",
        [acme.id],
    )
    with pytest.raises(SellerLiveElsewhere):
        await _insert(world, "bharat", seller="A1SHARED")


async def test_insert_live_seller_live_other_marketplace_ok(world: World) -> None:
    await _insert(world, "acme", seller="A1SHARED", marketplace="amazon")
    assert await _insert(world, "bharat", seller="A1SHARED", marketplace="flipkart") is not None


async def test_insert_live_seller_revoked_in_other_tenant_ok(world: World) -> None:
    acme = await _insert(world, "acme", seller="A1SHARED")
    await _revoke(world, "acme", acme.id)
    assert await _insert(world, "bharat", seller="A1SHARED") is not None


def test_seller_index_constant_matches_db(auth_db: Conn) -> None:
    from app.db.models.catalog_assets import UQ_MA_SELLER_LIVE

    rows = auth_db.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'marketplace_accounts'"
    ).fetchall()
    names = {str(r[0]) for r in rows}
    assert UQ_MA_SELLER_LIVE in names
    seller_keys = [r for r in rows if "(marketplace, seller_id)" in str(r[1])]
    assert [str(r[0]) for r in seller_keys] == [UQ_MA_SELLER_LIVE]


async def test_reconnect_keeps_id_clears_since(auth_db: Conn, world: World) -> None:
    from app.repositories.uow import tenant_uow

    account = await _insert(world, "acme")
    auth_db.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now(),"
        " last_refresh_error_code = 'AUTH_EXPIRED' WHERE id = %s",
        [account.id],
    )
    async with tenant_uow(world.tenant("acme")) as uow:
        await uow.marketplace_accounts.reconnect(
            account.id,
            sealed=_sealed(b"NEW"),
            expires_at=None,
            region="IN",
            scopes=("s2",),
            user_id=world.user("admin@acme.test"),
        )
    row = _row(auth_db, account.id)
    assert row["status"] == "active" and row["needs_reconnect_since"] is None
    assert row["last_refresh_error_code"] is None and row["scopes"] == ["s2"]
    assert bytes(row["token_enc"]).startswith(b"MKT1NEW")
    assert row["connected_by_user_id"] == world.user("admin@acme.test")


async def test_revoke_shreds_and_clears_since(auth_db: Conn, world: World) -> None:
    account = await _insert(world, "acme")
    auth_db.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now() "
        "WHERE id = %s",
        [account.id],
    )
    await _revoke(world, "acme", account.id)
    row = _row(auth_db, account.id)
    assert row["status"] == "revoked"
    assert row["token_enc"] is None and row["kms_key_version"] is None
    assert row["needs_reconnect_since"] is None and row["revoked_at"] is not None


async def _snapshot(world: World, account_id: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant("acme")) as uow:
        return await uow.marketplace_accounts.snapshot(account_id, margin=timedelta(hours=2))


async def _cas_tokens(world: World, account_id: uuid.UUID, seen: datetime) -> bool:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant("acme")) as uow:
        return await uow.marketplace_accounts.cas_tokens(account_id, seen, _sealed(b"CAS"), None)


async def test_cas_tokens_hit(auth_db: Conn, world: World) -> None:
    account = await _insert(world, "acme")
    snap = await _snapshot(world, account.id)
    assert snap.due and snap.token_enc is not None
    assert await _cas_tokens(world, account.id, snap.updated_at)
    row = _row(auth_db, account.id)
    assert bytes(row["token_enc"]).startswith(b"MKT1CAS") and row["last_refresh_at"] is not None


async def test_cas_tokens_stale_updated_at_false(auth_db: Conn, world: World) -> None:
    account = await _insert(world, "acme")
    snap = await _snapshot(world, account.id)
    auth_db.execute(
        "UPDATE marketplace_accounts SET settings = '{\"x\": 1}' WHERE id = %s", [account.id]
    )
    assert not await _cas_tokens(world, account.id, snap.updated_at)


async def test_cas_on_revoked_false(world: World) -> None:
    from app.repositories.uow import tenant_uow

    account = await _insert(world, "acme")
    snap = await _snapshot(world, account.id)
    await _revoke(world, "acme", account.id)
    assert not await _cas_tokens(world, account.id, snap.updated_at)
    async with tenant_uow(world.tenant("acme")) as uow:
        repo = uow.marketplace_accounts
        assert not await repo.cas_needs_reconnect(account.id, snap.updated_at)
        assert not await repo.cas_error(account.id, snap.updated_at, "MARKETPLACE_UNAVAILABLE")


async def test_snapshot_not_due_and_other_tenant(world: World) -> None:
    from app.repositories.uow import tenant_uow

    account = await _insert(world, "acme")
    async with tenant_uow(world.tenant("acme")) as uow:
        snap = await uow.marketplace_accounts.snapshot(account.id, margin=timedelta(minutes=5))
    assert snap is not None and not snap.due
    async with tenant_uow(world.tenant("bharat")) as uow:
        assert await uow.marketplace_accounts.snapshot(account.id, margin=timedelta(0)) is None


async def test_refresh_lock_second_holder_false(world: World) -> None:
    from app.repositories.marketplace_accounts import refresh_lock

    account_id = uuid.uuid4()
    async with refresh_lock(account_id) as first:
        assert first is True
        async with refresh_lock(account_id) as second:
            assert second is False
        async with refresh_lock(uuid.uuid4()) as other:
            assert other is True
    async with refresh_lock(account_id) as again:
        assert again is True


async def test_refresh_lock_released_on_error(world: World) -> None:
    from app.repositories.marketplace_accounts import refresh_lock

    account_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        async with refresh_lock(account_id) as held:
            assert held
            raise RuntimeError("boom")
    async with refresh_lock(account_id) as again:
        assert again is True


def _tasks(conn: Conn, account_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT status, cancelled_reason, answered_by_user_id, request_id FROM human_tasks "
        "WHERE marketplace_account_id = %s ORDER BY created_at",
        [account_id],
    ).fetchall()


async def test_open_reconnect_twice_one_task(auth_db: Conn, world: World) -> None:
    from app.repositories.uow import tenant_uow

    account = await _insert(world, "acme")
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.human_tasks.open_reconnect(account.id) is True
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.human_tasks.open_reconnect(account.id) is False
    assert [t[0] for t in _tasks(auth_db, account.id)] == ["open"]


async def test_apply_reconnect_guard_path(auth_db: Conn, world: World) -> None:
    from app.repositories.uow import tenant_uow

    account = await _insert(world, "acme")
    async with tenant_uow(world.tenant("acme")) as uow:
        await uow.human_tasks.open_reconnect(account.id)
    owner = world.user("owner@acme.test")
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.human_tasks.apply_reconnect(account.id, owner) == 1
    assert _tasks(auth_db, account.id)[0][:3] == ("applied", None, owner)
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.human_tasks.apply_reconnect(account.id, owner) == 0


async def test_cancel_reconnect_superseded(auth_db: Conn, world: World) -> None:
    from app.repositories.uow import tenant_uow

    account = await _insert(world, "acme")
    async with tenant_uow(world.tenant("acme")) as uow:
        await uow.human_tasks.open_reconnect(account.id)
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.human_tasks.cancel_reconnect(account.id) == 1
    assert _tasks(auth_db, account.id)[0][:2] == ("cancelled", "superseded")


async def _page(world: World, slug: str, limit: int, after: Any = None) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant(slug)) as uow:
        return await uow.marketplace_accounts.page(limit=limit, after=after)


async def test_page_keyset_two_pages_other_tenant_absent(world: World) -> None:
    a1 = await _insert(world, "acme", seller="A1", marketplace="amazon")
    await _revoke(world, "acme", a1.id)
    a2 = await _insert(world, "acme", seller="A2", marketplace="amazon")
    a3 = await _insert(world, "acme", seller="A3", marketplace="flipkart")
    await _insert(world, "bharat", seller="B1")
    first, more = await _page(world, "acme", 2)
    assert more is True and len(first) == 2
    second, more = await _page(world, "acme", 2, after=(first[-1].created_at, first[-1].id))
    assert more is False
    ids = [a.id for a in [*first, *second]]
    assert sorted(ids) == sorted([a1.id, a2.id, a3.id])
    assert len(set(ids)) == 3


def test_human_task_repository_subclasses_base() -> None:
    from app.repositories.base import TenantScopedRepository
    from app.repositories.human_tasks import HumanTaskRepository
    from app.repositories.marketplace_accounts import MarketplaceAccountRepository
    from app.repositories.oauth_states import OAuthStateRepository

    for repo in (HumanTaskRepository, MarketplaceAccountRepository, OAuthStateRepository):
        assert issubclass(repo, TenantScopedRepository)
