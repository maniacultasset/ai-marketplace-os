"""B19: the generation backlog's slot arithmetic, the terminal hook's non-blocking promotion and the
`waiting_for_slot` read, without a database (the integration suite `test_generation_live_cap.py`
covers the rows, the lock and the queue)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from app.services import generation_backlog
from app.services.jobs import waiting_for_slot


class FakeJobs:
    def __init__(self, live: int, waiting: int, *, locked: bool = False) -> None:
        self.live = live
        self.waiting = [SimpleNamespace(id=uuid.uuid4()) for _ in range(waiting)]
        self.locked = locked
        self.asked: list[int] = []

    async def count_live_generations(self) -> int:
        return self.live

    async def promote_waiting_generations(self, limit: int) -> list[Any]:
        self.asked.append(limit)
        taken, self.waiting = self.waiting[: max(limit, 0)], self.waiting[max(limit, 0) :]
        self.live += len(taken)
        return taken

    async def try_lock_generate_enqueue(self) -> bool:
        return not self.locked


class FakeSession:
    def __init__(self) -> None:
        self.savepoints = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        self.savepoints += 1
        try:
            yield
        except BaseException:
            self.rolled_back += 1
            raise


@pytest.fixture
def cap_ten(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("GENERATION_LIVE_JOBS_PER_TENANT", "10")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(("live", "waiting", "promoted"), [(7, 5, 3), (10, 5, 0), (0, 24, 10)])
async def test_promote_fills_only_the_free_slots(
    cap_ten: None, live: int, waiting: int, promoted: int
) -> None:
    jobs = FakeJobs(live, waiting)
    oldest = [job.id for job in jobs.waiting[:promoted]]
    assert await generation_backlog.promote(jobs) == oldest  # type: ignore[arg-type]
    assert jobs.live == max(live, 10) and len(jobs.waiting) == waiting - promoted


async def test_promote_past_the_cap_promotes_nothing(cap_ten: None) -> None:
    """Jobs enqueued outside the cap (a reviewer's reject-and-regenerate, an auto fix) can put a
    tenant above it: the free count is negative and nothing is promoted."""
    jobs = FakeJobs(12, 3)
    assert await generation_backlog.promote(jobs) == []  # type: ignore[arg-type]
    assert jobs.asked == [-2] and len(jobs.waiting) == 3


def _hook_jobs(monkeypatch: pytest.MonkeyPatch, jobs: FakeJobs) -> None:
    monkeypatch.setattr(generation_backlog, "JobRunRepository", lambda _session, _tenant: jobs)


async def test_terminal_promotion_takes_the_slot(
    cap_ten: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = FakeJobs(9, 2)
    _hook_jobs(monkeypatch, jobs)
    session = FakeSession()
    job: Any = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    await generation_backlog.promote_on_terminal(session, job)
    assert session.savepoints == 1 and jobs.asked == [1] and len(jobs.waiting) == 1


async def test_terminal_promotion_never_waits_for_the_lock(
    cap_ten: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = FakeJobs(0, 2, locked=True)
    _hook_jobs(monkeypatch, jobs)
    job: Any = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    await generation_backlog.promote_on_terminal(FakeSession(), job)
    assert jobs.asked == [] and len(jobs.waiting) == 2


async def test_terminal_promotion_failure_never_undoes_the_terminal_write(
    cap_ten: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenJobs(FakeJobs):
        async def promote_waiting_generations(self, limit: int) -> list[Any]:
            raise RuntimeError("queue down")

    jobs = BrokenJobs(0, 1)
    _hook_jobs(monkeypatch, jobs)
    session = FakeSession()
    job: Any = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    await generation_backlog.promote_on_terminal(session, job)  # does not raise
    assert session.rolled_back == 1


@pytest.mark.parametrize(
    ("kind", "status", "entry", "waiting"),
    [
        ("listing_generate", "queued", None, True),
        ("listing_generate", "queued", "17", False),
        ("listing_generate", "running", "17", False),
        ("listing_generate", "cancelled", None, False),
        ("bulk_import", "queued", None, False),
    ],
)
def test_waiting_for_slot_reads_the_row(
    kind: str, status: str, entry: str | None, waiting: bool
) -> None:
    job: Any = SimpleNamespace(kind=kind, status=status, queue_job_id=entry)
    assert waiting_for_slot(job) is waiting
