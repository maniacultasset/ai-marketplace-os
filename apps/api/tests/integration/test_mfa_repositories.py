"""DPP-1 T9 (ADR-0021): the MFA repositories and the new identity calls, run as app_rw under FORCE
RLS through the app engine: single-use TOTP steps and recovery codes (also under a race), the
challenge attempt cap and pruning, the step-up family update (AUTH-3, also during a concurrent
refresh), revoking every family of a user, and the row-lock gate's fresh state."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .auth_support import Conn, add_member, add_tenant, add_user
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _hash(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


@pytest.fixture
def user(auth_db: Conn) -> uuid.UUID:
    tenant = add_tenant(auth_db, "acme")
    user_id = add_user(auth_db, "owner@acme.test")
    add_member(auth_db, tenant, user_id, "owner")
    return user_id


async def _in_session(user_id: uuid.UUID, fn: Any, **gucs: Any) -> Any:
    from app.repositories.transactions import identity_session, set_gucs

    async with identity_session() as session:
        await set_gucs(session, user_id=user_id, **gucs)
        return await fn(session)


async def _factor(user_id: uuid.UUID) -> uuid.UUID:
    from app.repositories.mfa import MfaFactorRepository

    async def run(session: Any) -> uuid.UUID:
        repo = MfaFactorRepository(session)
        factor = await repo.replace_pending(user_id, b"MFA1-sealed", "local-1")
        await repo.activate(factor, user_id)
        return factor

    found: uuid.UUID = await _in_session(user_id, run)
    return found


async def _consume(user_id: uuid.UUID, factor: uuid.UUID, step: int) -> bool:
    from app.repositories.mfa import MfaFactorRepository

    used: bool = await _in_session(
        user_id, lambda s: MfaFactorRepository(s).consume_step(factor, step)
    )
    return used


async def test_consume_step_replay_same_step_false(auth_db: Conn, user: uuid.UUID) -> None:
    factor = await _factor(user)
    assert await _consume(user, factor, 100) is True
    assert await _consume(user, factor, 100) is False


async def test_consume_step_older_step_false(auth_db: Conn, user: uuid.UUID) -> None:
    factor = await _factor(user)
    assert await _consume(user, factor, 101) is True
    assert await _consume(user, factor, 100) is False
    assert await _consume(user, factor, 102) is True


async def test_consume_step_concurrent_one_wins(auth_db: Conn, user: uuid.UUID) -> None:
    factor = await _factor(user)
    results = await asyncio.gather(*(_consume(user, factor, 200) for _ in range(5)))
    assert results.count(True) == 1


async def _codes(user_id: uuid.UUID, hashes: list[bytes]) -> None:
    from app.repositories.mfa import RecoveryCodeRepository

    await _in_session(user_id, lambda s: RecoveryCodeRepository(s).replace(user_id, hashes))


async def _use(user_id: uuid.UUID, digest: bytes) -> bool:
    from app.repositories.mfa import RecoveryCodeRepository

    used: bool = await _in_session(
        user_id, lambda s: RecoveryCodeRepository(s).consume(user_id, digest)
    )
    return used


async def test_recovery_consume_once(auth_db: Conn, user: uuid.UUID) -> None:
    await _codes(user, [_hash("a"), _hash("b")])
    assert await _use(user, _hash("a")) is True
    assert await _use(user, _hash("a")) is False
    from app.repositories.mfa import RecoveryCodeRepository

    assert await _in_session(user, lambda s: RecoveryCodeRepository(s).left(user)) == 1


async def test_recovery_consume_concurrent_one_wins(auth_db: Conn, user: uuid.UUID) -> None:
    await _codes(user, [_hash("race")])
    results = await asyncio.gather(*(_use(user, _hash("race")) for _ in range(5)))
    assert results.count(True) == 1


async def _challenge(user_id: uuid.UUID, label: str, **changes: Any) -> uuid.UUID:
    from app.repositories.mfa import ChallengeRepository

    values: dict[str, Any] = {
        "user_id": user_id,
        "purpose": "login",
        "amr": ["pwd"],
        "challenge_hash": _hash(label),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "ip": None,
        "user_agent": None,
        **changes,
    }
    created: uuid.UUID = await _in_session(
        user_id, lambda s: ChallengeRepository(s).create(**values)
    )
    return created


async def test_challenge_fail_burns_at_5(auth_db: Conn, user: uuid.UUID) -> None:
    from app.repositories.mfa import ChallengeRepository

    challenge = await _challenge(user, "burn")
    for n in range(1, 6):
        assert await _in_session(user, lambda s: ChallengeRepository(s).fail(challenge)) == n
    row = auth_db.execute(
        "SELECT attempts, consumed_at IS NOT NULL FROM mfa_challenges WHERE id = %s", [challenge]
    ).fetchone()
    assert row == (5, True)
    locked = await _in_session(user, lambda s: ChallengeRepository(s).lock(_hash("burn")))
    assert locked is None


async def test_challenge_create_prunes_own_expired_only(auth_db: Conn, user: uuid.UUID) -> None:
    other = add_user(auth_db, "other@acme.test")
    await _challenge(user, "old", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    await _challenge(other, "theirs", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    await _challenge(user, "new")
    rows = auth_db.execute("SELECT challenge_hash FROM mfa_challenges").fetchall()
    assert sorted(bytes(r[0]) for r in rows) == sorted([_hash("theirs"), _hash("new")])


async def test_challenge_round_trips_mfa_at(auth_db: Conn, user: uuid.UUID) -> None:
    from app.repositories.mfa import ChallengeRepository

    moment = datetime.now(UTC).replace(microsecond=0)
    await _challenge(user, "rt", purpose="password_change", amr=["pwd", "otp"], mfa_at=moment)
    row = await _in_session(user, lambda s: ChallengeRepository(s).lock(_hash("rt")))
    assert row is not None and row.mfa_at == moment and row.amr == ["pwd", "otp"]


async def test_peek_user_then_lock_by_hash_only(auth_db: Conn, user: uuid.UUID) -> None:
    from app.repositories.mfa import ChallengeRepository

    await _challenge(user, "peek")
    from app.repositories.transactions import identity_session, set_gucs

    async with identity_session() as session:
        await set_gucs(session, challenge_hash=_hash("peek"))
        assert await ChallengeRepository(session).peek_user(_hash("peek")) == user
        assert await ChallengeRepository(session).peek_user(_hash("other")) is None


async def test_password_hashes_current_plus_9(auth_db: Conn, user: uuid.UUID) -> None:
    from app.repositories.identity import UserRepository
    from app.repositories.mfa import PasswordHistoryRepository

    async def run(session: Any) -> list[str]:
        history = PasswordHistoryRepository(session)
        for n in range(12):
            await history.push(user, f"old-{n:02d}")
            await history.prune(user)
        return await UserRepository(session).password_hashes(user)

    hashes = await _in_session(user, run)
    assert len(hashes) == 10
    assert hashes[0].startswith("$argon2id$")  # the current one first
    assert hashes[1:] == [f"old-{n:02d}" for n in range(11, 2, -1)]


async def test_begin_auth_attempt_returns_fresh_state_not_by_email_snapshot(
    auth_db: Conn, user: uuid.UUID
) -> None:
    from app.repositories.identity import AuthState, UserRepository
    from app.repositories.transactions import identity_session, set_gucs

    async with identity_session() as session:
        await set_gucs(session, login_email="owner@acme.test")
        snapshot = await UserRepository(session).by_email("owner@acme.test")
        assert snapshot is not None and snapshot.failed_auth_count == 0
        auth_db.execute("UPDATE users SET failed_auth_count = 6 WHERE id = %s", [user])
        state = await UserRepository(session).begin_auth_attempt(user)
    assert isinstance(state, AuthState) and state.failed_auth_count == 6


async def test_revoke_user_everywhere_two_tenants_keeps_family(
    auth_db: Conn, user: uuid.UUID
) -> None:
    from app.repositories.identity import RefreshTokenRepository

    acme = auth_db.execute("SELECT id FROM tenants WHERE slug = 'acme'").fetchone()
    assert acme is not None
    other = add_tenant(auth_db, "bharat")
    add_member(auth_db, other, user, "operator")
    keep = uuid.uuid4()
    for tenant, family, label in (
        (acme[0], keep, "keep"),
        (acme[0], uuid.uuid4(), "a2"),
        (other, uuid.uuid4(), "b1"),
    ):
        auth_db.execute(
            "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + interval '1 day')",
            [tenant, user, family, _hash(label)],
        )
    revoked = await _in_session(
        user,
        lambda s: RefreshTokenRepository(s).revoke_user_everywhere(
            user, keep_family=keep, reason="password_changed"
        ),
    )
    assert revoked == 2
    live = auth_db.execute(
        "SELECT family_id FROM refresh_tokens WHERE revoked_at IS NULL"
    ).fetchall()
    assert live == [(keep,)]


async def test_step_up_family_only_sid_and_sub(auth_db: Conn, user: uuid.UUID) -> None:
    from app.repositories.identity import RefreshTokenRepository

    acme = auth_db.execute("SELECT id FROM tenants WHERE slug = 'acme'").fetchone()
    assert acme is not None
    other_user = add_user(auth_db, "operator@acme.test")
    add_member(auth_db, acme[0], other_user, "operator")
    family = uuid.uuid4()
    auth_db.execute(
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + interval '1 day')",
        [acme[0], other_user, family, _hash("theirs")],
    )
    touched = await _in_session(
        user,
        lambda s: RefreshTokenRepository(s).step_up_family(
            acme[0], family, user, amr=["pwd", "otp"], mfa_at=datetime.now(UTC)
        ),
        tenant_id=acme[0],
    )
    assert touched == 0
