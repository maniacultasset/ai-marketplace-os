"""S13 A12 (DW6, AC9; DATABASE.md:978-981): the Idempotency-Key protocol in one tenant
transaction, as app_rw: replay, mismatch, another member, concurrency, expiry, stored 422s,
tenant independence and rollback."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.db.session import unit_of_work
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.services.idempotency import idempotent, request_hash
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
PATH = "/api/v1/things"


def _principal(jw: JobWorld, slug: str = "acme", role: str = "owner") -> Principal:
    email = f"{role}@{slug}.test"
    return Principal(
        user_id=jw.world.user(email),
        tenant_id=jw.tenant(slug),
        role=role,  # type: ignore[arg-type]
        email=email,
        jti=str(uuid.uuid4()),
    )


def _enqueue_handler(principal: Principal, calls: list[int], delay: float = 0) -> Any:
    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        calls.append(1)
        if delay:
            await asyncio.sleep(delay)
        done = await JobRunRepository(session, principal.tenant_id).enqueue(
            EnqueueSpec(
                kind=JobKind.BULK_IMPORT,
                entity_type="import",
                entity_id=uuid.uuid4(),
                created_by=principal.user_id,
            )
        )
        return 202, {"job_id": str(done.job.id)}

    return handler


def _jobs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM job_runs")


async def test_idempotency_replay_same_body(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)
    calls: list[int] = []
    first = await idempotent(p, "k1", "POST", PATH, b"{}", _enqueue_handler(p, calls))
    again = await idempotent(p, "k1", "POST", PATH, b"{}", _enqueue_handler(p, calls))
    assert first == again and first[0] == 202
    assert calls == [1] and _jobs(jobs_db) == 1


async def test_idempotency_other_body_409(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)
    calls: list[int] = []
    await idempotent(p, "k1", "POST", PATH, b'{"a":1}', _enqueue_handler(p, calls))
    with pytest.raises(ConflictError) as caught:
        await idempotent(p, "k1", "POST", PATH, b'{"a":2}', _enqueue_handler(p, calls))
    assert caught.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED
    with pytest.raises(ConflictError):
        await idempotent(p, "k1", "POST", "/api/v1/other", b'{"a":1}', _enqueue_handler(p, calls))
    assert calls == [1] and _jobs(jobs_db) == 1


async def test_idempotency_other_member_409(jobs_db: JobWorld) -> None:
    owner, operator = _principal(jobs_db), _principal(jobs_db, role="operator")
    calls: list[int] = []
    await idempotent(owner, "k1", "POST", PATH, b"{}", _enqueue_handler(owner, calls))
    with pytest.raises(ConflictError) as caught:
        await idempotent(operator, "k1", "POST", PATH, b"{}", _enqueue_handler(operator, calls))
    assert caught.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED
    assert calls == [1]


async def test_idempotency_two_concurrent_one_job(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)
    calls: list[int] = []
    results = await asyncio.gather(
        idempotent(p, "kc", "POST", PATH, b"{}", _enqueue_handler(p, calls, delay=0.3)),
        idempotent(p, "kc", "POST", PATH, b"{}", _enqueue_handler(p, calls, delay=0.3)),
    )
    assert results[0] == results[1]
    assert calls == [1] and _jobs(jobs_db) == 1


async def test_idempotency_expired_key_is_new(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)
    calls: list[int] = []
    first = await idempotent(p, "k1", "POST", PATH, b"{}", _enqueue_handler(p, calls))
    jobs_db.conn.execute("UPDATE idempotency_keys SET expires_at = now() - interval '1 second'")
    second = await idempotent(p, "k1", "POST", PATH, b"{}", _enqueue_handler(p, calls))
    assert first != second and calls == [1, 1] and _jobs(jobs_db) == 2
    live = jobs_db.conn.execute("SELECT expires_at > now() FROM idempotency_keys").fetchone()
    assert live == (True,)


async def test_idempotency_stores_422(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)
    calls: list[int] = []

    async def bad(session: Any) -> tuple[int, dict[str, Any]]:
        calls.append(1)
        return 422, {"error": "IMPORT_FAILED"}

    assert await idempotent(p, "k4", "POST", PATH, b"x", bad) == (422, {"error": "IMPORT_FAILED"})
    assert await idempotent(p, "k4", "POST", PATH, b"x", bad) == (422, {"error": "IMPORT_FAILED"})
    assert calls == [1]


async def test_idempotency_refuses_unstorable_status(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)

    async def wrong(session: Any) -> tuple[int, dict[str, Any]]:
        return 409, {}

    with pytest.raises(ValueError, match="raise errors instead"):
        await idempotent(p, "k9", "POST", PATH, b"x", wrong)
    assert count(jobs_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


async def test_idempotency_same_key_other_tenant_independent(jobs_db: JobWorld) -> None:
    a, b = _principal(jobs_db), _principal(jobs_db, "bharat")
    calls: list[int] = []
    ra = await idempotent(a, "shared", "POST", PATH, b"{}", _enqueue_handler(a, calls))
    rb = await idempotent(b, "shared", "POST", PATH, b"{}", _enqueue_handler(b, calls))
    assert ra != rb and calls == [1, 1]
    assert count(jobs_db.conn, "SELECT count(*) FROM idempotency_keys") == 2


async def test_idempotency_rollback_leaves_no_key(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)

    async def boom(session: Any) -> tuple[int, dict[str, Any]]:
        await _enqueue_handler(p, [])(session)
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "job_runs")

    with pytest.raises(ConflictError):
        await idempotent(p, "kr", "POST", PATH, b"{}", boom)
    assert count(jobs_db.conn, "SELECT count(*) FROM idempotency_keys") == 0
    assert _jobs(jobs_db) == 0
    calls: list[int] = []
    await idempotent(p, "kr", "POST", PATH, b"{}", _enqueue_handler(p, calls))
    assert calls == [1]


async def test_idempotency_peek_replay_mismatch_none(jobs_db: JobWorld) -> None:
    p = _principal(jobs_db)
    await idempotent(p, "kp", "POST", PATH, b"{}", _enqueue_handler(p, []))
    digest, other = request_hash("POST", PATH, b"{}"), request_hash("POST", PATH, b"[]")
    async with unit_of_work(p.tenant_id) as session:
        keys = IdempotencyKeyRepository(session, p.tenant_id)
        assert isinstance(await keys.peek("kp", f"POST {PATH}", digest, p.user_id), Replay)
        assert isinstance(await keys.peek("kp", f"POST {PATH}", other, p.user_id), Mismatch)
        assert await keys.peek("absent", f"POST {PATH}", digest, p.user_id) is None
