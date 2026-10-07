"""The S13 job-spine test world: the auth world (`acme`, `bharat`, one member per role) on an
emptied test database, the app under test as `app_rw`, and an empty procrastinate queue.

Rows are built and read back on the admin (superuser, autocommit) connection, so a row is seen
there only if the code under test committed it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from .auth_support import World, build_world

Conn = psycopg.Connection[tuple[Any, ...]]
QUEUE_TABLES = (
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_jobs",
    "procrastinate_workers",
)


def reset_queue(conn: Conn) -> None:
    conn.execute(f"TRUNCATE {', '.join(QUEUE_TABLES)} RESTART IDENTITY CASCADE")


@dataclass
class JobWorld:
    conn: Conn
    world: World

    def tenant(self, slug: str) -> uuid.UUID:
        return self.world.tenant(slug)

    def owner(self, slug: str) -> uuid.UUID:
        return self.world.user(f"owner@{slug}.test")


@pytest.fixture
async def jobs_db(auth_db: Conn) -> AsyncIterator[JobWorld]:
    from app.db.queue import get_queue_app

    reset_queue(auth_db)
    get_queue_app.cache_clear()
    yield JobWorld(auth_db, build_world(auth_db))
    get_queue_app.cache_clear()


@pytest.fixture
def override_body() -> Iterator[Any]:
    """Register fake bodies for the test only; the registry is restored afterwards."""
    from app.workers import registry

    saved = dict(registry.REGISTRY)

    def install(kind: Any, body: Any) -> None:
        registry.register(kind, body)

    yield install
    registry.REGISTRY.clear()
    registry.REGISTRY.update(saved)


def insert_job(conn: Conn, tenant_id: uuid.UUID, **columns: Any) -> uuid.UUID:
    """A queued `bulk_import` job on an `import` entity, written by the admin login."""
    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "kind": "bulk_import",
        "entity_type": "import",
        "entity_id": uuid.uuid4(),
        "payload_json": {},
        "correlation_id": uuid.uuid4(),
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    row = conn.execute(
        f"INSERT INTO job_runs ({names}) VALUES ({marks}) RETURNING id",  # noqa: S608
        [Jsonb(v) if isinstance(v, dict) else v for v in values.values()],
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def job_row(conn: Conn, job_id: uuid.UUID) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM job_runs WHERE id = %s", [job_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None, f"no job_runs row {job_id}"
    return dict(zip(names, row, strict=True))


def queue_row(conn: Conn, queue_job_id: str | int) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT id, queue_name, task_name, status, args, attempts, scheduled_at "
        "FROM procrastinate_jobs WHERE id = %s",
        [int(queue_job_id)],
    )
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None, f"no procrastinate_jobs row {queue_job_id}"
    return dict(zip(names, row, strict=True))


def count(conn: Conn, query: str, params: list[Any] | None = None) -> int:
    row = conn.execute(query, params or []).fetchone()
    assert row is not None
    return int(row[0])


# ---- a real worker in-process (A6-A9) ----


@asynccontextmanager
async def running_worker(
    *,
    queues: list[str] | None = None,
    graceful_s: float = 0.5,
    poll_s: float = 0.2,
    concurrency: int = 4,
    name: str = "test-worker",
) -> AsyncIterator[asyncio.Task[None]]:
    """A procrastinate worker on a fresh queue app (as app_rw) with the six kind tasks, stopped
    on exit the way SIGTERM stops it: running jobs get `graceful_s`, then are aborted."""
    from app.db.queue import new_queue_app
    from app.workers.tasks import register_tasks

    app = new_queue_app()
    register_tasks(app)
    options: dict[str, Any] = {} if queues is None else {"queues": queues}
    async with app.open_async():
        task = asyncio.create_task(
            app.run_worker_async(
                **options,
                name=name,
                wait=True,
                concurrency=concurrency,
                fetch_job_polling_interval=poll_s,
                abort_job_polling_interval=poll_s,
                shutdown_graceful_timeout=graceful_s,
                install_signal_handlers=False,
                listen_notify=True,
                update_heartbeat_interval=1.0,
            )
        )
        try:
            yield task
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def eventually(check: Callable[[], bool], timeout_s: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


# ---- API callers (A13-A14) ----


def as_member(jw: JobWorld, email: str) -> dict[str, str]:
    from .auth_support import bearer, tokens_for

    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(jw.world, email, slug))


def bad_token(jw: JobWorld, variant: str) -> dict[str, str]:
    """An access token for owner@acme.test that the API must refuse: `expired`,
    `bad_signature` (re-signed with another key) or `wrong_typ` (a refresh token)."""
    import time

    import jwt
    from tests.conftest import CI_JWT_SECRET

    email = "owner@acme.test"
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(jw.world.user(email)),
        "tid": str(jw.tenant("acme")),
        "roles": ["owner"],
        "email": email,
        "typ": "access",
        "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000",
        "aud": "ai-marketplace-os",
        "iat": now,
        "exp": now + 600,
    }
    secret = CI_JWT_SECRET
    if variant == "expired":
        claims.update(iat=now - 3600, exp=now - 1)
    elif variant == "wrong_typ":
        claims["typ"] = "refresh"
    elif variant == "bad_signature":
        secret = "another_secret_that_is_at_least_32_chars"
    else:
        raise ValueError(variant)
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"}
