"""SI5 T4 (ADR-0029): the invitation, reset-link and tenant-admin repositories, run as app_rw
under FORCE RLS. The world is built on the admin connection; every read and write below goes
through the app engine, so a missing GUC shows as a missing row."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

import pytest

from .auth_support import Conn, add_member, add_tenant, add_user, build_world
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

WEEK = timedelta(days=7)


def _hash(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


async def _create(tenant: uuid.UUID, inviter: uuid.UUID, email: str, label: str) -> Any:
    from app.repositories.invitations import InvitationRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(tenant, user_id=inviter) as session:
        return await InvitationRepository(session, tenant).create(
            email=email, role="reviewer", token_hash=_hash(label), invited_by=inviter, ttl=WEEK
        )


async def _page(tenant: uuid.UUID, user: uuid.UUID, **kwargs: Any) -> list[Any]:
    from app.repositories.invitations import InvitationRepository
    from app.repositories.transactions import unit_of_work

    async with unit_of_work(tenant, user_id=user) as session:
        return await InvitationRepository(session, tenant).page(**kwargs)


async def test_status_is_derived_with_expiry_at_now(auth_db: Conn) -> None:
    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    row = await _create(acme, owner, "new@acme.test", "s1")
    assert row.status == "pending"
    auth_db.execute("UPDATE invitations SET expires_at = now() WHERE id = %s", [row.id])
    (found,) = await _page(acme, owner, status=None, limit=10, after=None)
    assert found.status == "expired"
    assert await _page(acme, owner, status="pending", limit=10, after=None) == []
    assert [r.id for r in await _page(acme, owner, status="expired", limit=10, after=None)] == [
        row.id
    ]


async def test_revoke_open_for_email_frees_the_pending_slot_even_expired(auth_db: Conn) -> None:
    from app.repositories.invitations import InvitationRepository
    from app.repositories.transactions import unit_of_work

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    first = await _create(acme, owner, "again@acme.test", "r1")
    auth_db.execute(
        "UPDATE invitations SET expires_at = now() - interval '1 day' WHERE id = %s", [first.id]
    )
    async with unit_of_work(acme, user_id=owner) as session:
        repo = InvitationRepository(session, acme)
        assert await repo.open_count() == 0
        assert await repo.revoke_open_for_email("again@acme.test") == 1
        second = await repo.create(
            email="again@acme.test",
            role="admin",
            token_hash=_hash("r2"),
            invited_by=owner,
            ttl=WEEK,
        )
        assert await repo.open_count() == 1
    statuses = {r.id: r.status for r in await _page(acme, owner, status=None, limit=10, after=None)}
    assert statuses == {first.id: "revoked", second.id: "pending"}


async def test_keyset_page_has_no_duplicates(auth_db: Conn) -> None:
    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    made = {(await _create(acme, owner, f"p{n}@acme.test", f"k{n}")).id for n in range(5)}
    seen: list[uuid.UUID] = []
    after = None
    for _ in range(4):
        rows = await _page(acme, owner, status=None, limit=2, after=after)
        if not rows:
            break
        seen += [r.id for r in rows]
        after = (rows[-1].created_at, rows[-1].id)
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == made


async def test_tenant_isolation_invites_and_links(auth_db: Conn) -> None:
    from app.repositories.invitations import InvitationRepository
    from app.repositories.password_reset_links import ResetLinkRepository
    from app.repositories.transactions import unit_of_work

    world = build_world(auth_db)
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    a_owner, b_owner = world.user("owner@acme.test"), world.user("owner@bharat.test")
    a_invite = await _create(acme, a_owner, "x@acme.test", "iso1")
    async with unit_of_work(acme, user_id=a_owner) as session:
        link = await ResetLinkRepository(session, acme).create(
            user_id=world.user("reviewer@acme.test"),
            token_hash=_hash("iso-link"),
            issued_by=a_owner,
            ttl=timedelta(hours=24),
        )
    async with unit_of_work(bharat, user_id=b_owner) as session:
        invites = InvitationRepository(session, bharat)
        assert await invites.page(status=None, limit=10, after=None) == []
        assert await invites.lock(a_invite.id) is None
        assert await invites.get(a_invite.id) is None
        assert await invites.open_count() == 0
        assert await invites.revoke_open_for_email("x@acme.test") == 0
        assert await invites.email_is_member("owner@acme.test") is False
        links = ResetLinkRepository(session, bharat)
        assert await links.lock(link.id) is None
        assert await links.revoke_open(world.user("reviewer@acme.test")) == 0
    async with unit_of_work(acme, user_id=a_owner) as session:
        assert await InvitationRepository(session, acme).email_is_member("Owner@Acme.test")
        assert (await ResetLinkRepository(session, acme).lock(link.id)) is not None


async def test_invitation_by_hash(auth_db: Conn) -> None:
    from app.repositories.identity import UserRepository
    from app.repositories.transactions import identity_session

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    row = await _create(acme, owner, "hash@acme.test", "h1")
    async with identity_session() as session:
        users = UserRepository(session)
        assert await users.invitation_by_hash(_hash("wrong")) is None
        found = await users.invitation_by_hash(_hash("h1"))
    assert found is not None
    assert (found.id, found.tenant_id, found.email, found.role) == (
        row.id,
        acme,
        "hash@acme.test",
        "reviewer",
    )


async def test_reset_link_lifecycle_and_hash(auth_db: Conn) -> None:
    from app.repositories.identity import UserRepository
    from app.repositories.password_reset_links import ResetLinkRepository
    from app.repositories.transactions import identity_session, unit_of_work

    world = build_world(auth_db)
    acme, owner = world.tenant("acme"), world.user("owner@acme.test")
    target = world.user("reviewer@acme.test")
    async with unit_of_work(acme, user_id=owner) as session:
        links = ResetLinkRepository(session, acme)
        first = await links.create(
            user_id=target, token_hash=_hash("l1"), issued_by=owner, ttl=timedelta(hours=24)
        )
        assert await links.revoke_open(target) == 1
        second = await links.create(
            user_id=target, token_hash=_hash("l2"), issued_by=owner, ttl=timedelta(hours=24)
        )
    async with identity_session() as session:
        users = UserRepository(session)
        assert await users.reset_link_by_hash(_hash("nope")) is None
        found = await users.reset_link_by_hash(_hash("l2"))
    assert found is not None and found.id == second.id and found.tenant_id == acme
    async with unit_of_work(acme, user_id=owner) as session:
        links = ResetLinkRepository(session, acme)
        old = await links.lock(first.id)
        assert old is not None and old.live is False
        live = await links.lock(second.id)
        assert live is not None and live.live is True
        await links.mark_used(second.id)
        used = await links.lock(second.id)
        assert used is not None and used.live is False


async def test_tenant_admin_page_needs_the_admin_guc(auth_db: Conn) -> None:
    from app.repositories.identity import TenantAdminRepository
    from app.repositories.transactions import identity_session, set_gucs

    world = build_world(auth_db)
    staff = add_tenant(auth_db, "kuinn-staff")
    admin = add_user(auth_db, "admin@kuinn.test")
    add_member(auth_db, staff, admin, "owner")
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE id = %s", [admin])
    async with identity_session() as session:
        await set_gucs(session, user_id=admin)
        repo = TenantAdminRepository(session)
        rows = await repo.page(status=None, limit=2, after=None)
        more = await repo.page(status=None, limit=5, after=(rows[-1].created_at, rows[-1].id))
        active = await repo.page(status="active", limit=10, after=None)
    ids = [r.id for r in rows + more]
    assert set(ids) == {world.tenant("acme"), world.tenant("bharat"), staff}
    assert len(ids) == len(set(ids))
    assert {r.status for r in active} == {"active"}
    async with identity_session() as session:
        await set_gucs(session, user_id=world.user("owner@acme.test"))
        seen = await TenantAdminRepository(session).page(status=None, limit=10, after=None)
    assert [r.id for r in seen] == [world.tenant("acme")]
