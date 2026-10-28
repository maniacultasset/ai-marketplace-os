"""The marketplace account purge and the daily maintenance run (Amazon DPP 1.7; migration 0011b),
without a database: the due-account query's shape, the purge transaction's order (objects deleted
before the commit, a failed delete rolls back), the pass's failure isolation and the run's exit
code. The SQL function itself is covered by tests/integration/test_0011b_account_purge.py."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from app.repositories import account_purge
from app.workers import maintenance, periodic
from sqlalchemy.dialects import postgresql

SQL = (
    Path(account_purge.__file__).resolve().parents[2] / "alembic/sql/0011b/01_account_purge.up.sql"
)


def test_window_matches_the_sql_literal() -> None:
    """25 days in Python and in `purge_marketplace_account`, both statuses (DPP 1.7: 30 days)."""
    text = SQL.read_text(encoding="utf-8")
    assert re.findall(r"interval '(\d+) days'", text)[:2] == ["25", "25"]
    assert account_purge.PURGE_AFTER_DAYS == 25
    assert account_purge.PURGE_AFTER_DAYS < 30


def test_due_query_covers_revoked_and_disconnected_only() -> None:
    class Capture:
        stmt: Any = None

        async def execute(self, stmt: Any) -> Any:
            Capture.stmt = stmt

            class Rows:
                def all(self) -> list[Any]:
                    return []

            return Rows()

    import asyncio

    asyncio.run(account_purge.due_accounts(Capture(), limit=7))
    sql = str(Capture.stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "marketplace_accounts.status = " in sql
    assert "marketplace_accounts.revoked_at <= " in sql
    assert "marketplace_accounts.needs_reconnect_since <= " in sql
    assert "token_enc" not in sql  # never reads a credential column
    assert "LIMIT" in sql


class FakeTransaction:
    def __init__(self, log: list[str], row: dict[str, Any]) -> None:
        self.log = log
        self.row = row

    async def __aenter__(self) -> FakeTransaction:
        self.log.append("begin")
        return self

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        self.log.append("rollback" if exc_type else "commit")

    async def execute(self, stmt: Any, params: dict[str, Any]) -> Any:
        self.log.append(f"purge {params['t']} {params['a']}")
        row = self.row

        class Result:
            def scalar_one(self) -> dict[str, Any]:
                return row

        return Result()


class FakeEngine:
    def __init__(self, row: dict[str, Any]) -> None:
        self.log: list[str] = []
        self.row = row

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self.log, self.row)


ROW: dict[str, Any] = {
    "row_counts": {"listings": 2, "listing_submissions": 3, "marketplace_accounts": 1},
    "object_keys": ["t/submissions/s1/payload.json", "t/submissions/s1/raw.jsonl"],
}


async def test_objects_are_deleted_before_the_commit() -> None:
    engine = FakeEngine(ROW)
    deleted: list[str] = []

    async def delete(keys: Sequence[str]) -> None:
        engine.log.append("delete")
        deleted.extend(keys)

    t, a = uuid.uuid4(), uuid.uuid4()
    result = await account_purge.Purger(engine).purge(t, a, delete)  # type: ignore[arg-type]
    assert engine.log == ["begin", f"purge {t} {a}", "delete", "commit"]
    assert deleted == ROW["object_keys"]
    assert result == account_purge.PurgeResult(row_counts=ROW["row_counts"], objects=2)


async def test_a_failed_object_delete_rolls_the_purge_back() -> None:
    engine = FakeEngine(ROW)

    async def delete(keys: Sequence[str]) -> None:
        raise RuntimeError("storage down")

    with pytest.raises(RuntimeError):
        await account_purge.Purger(engine).purge(uuid.uuid4(), uuid.uuid4(), delete)  # type: ignore[arg-type]
    assert engine.log[-1] == "rollback"


async def test_no_objects_means_no_delete_call() -> None:
    engine = FakeEngine({"row_counts": {"marketplace_accounts": 1}, "object_keys": []})

    async def delete(keys: Sequence[str]) -> None:
        raise AssertionError("no delete expected")

    result = await account_purge.Purger(engine).purge(uuid.uuid4(), uuid.uuid4(), delete)  # type: ignore[arg-type]
    assert result.objects == 0 and engine.log[-1] == "commit"


def test_purger_needs_the_migrator_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class S:
        alembic_database_url = None

    monkeypatch.setattr(account_purge, "get_settings", lambda: S())

    async def open_it() -> None:
        async with account_purge.purger():
            pass

    import asyncio

    with pytest.raises(RuntimeError, match="ALEMBIC_DATABASE_URL"):
        asyncio.run(open_it())


# ---- the daily pass ----


class FakeStorage:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete(self, bucket: str, key: str) -> None:
        self.deleted.append((bucket, key))


@asynccontextmanager
async def _scope() -> AsyncIterator[object]:
    yield object()


def _wire(
    monkeypatch: pytest.MonkeyPatch, due: list[tuple[uuid.UUID, uuid.UUID]], bad: set[uuid.UUID]
) -> tuple[FakeStorage, list[str]]:
    storage = FakeStorage()
    opened: list[str] = []

    async def fake_due(session: Any, *, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
        assert limit == periodic.ACCOUNT_PURGE_BATCH
        return due

    class FakePurger:
        async def purge(self, t: uuid.UUID, a: uuid.UUID, delete: Any) -> Any:
            if a in bad:
                raise RuntimeError("GRD01")
            await delete([f"{t}/submissions/{a}/raw.jsonl"])
            return account_purge.PurgeResult(row_counts={"listings": 1}, objects=1)

    @asynccontextmanager
    async def fake_purger() -> AsyncIterator[FakePurger]:
        opened.append("purger")
        yield FakePurger()

    monkeypatch.setattr(periodic, "platform_scope", _scope)
    monkeypatch.setattr(account_purge, "due_accounts", fake_due)
    monkeypatch.setattr(account_purge, "purger", fake_purger)
    monkeypatch.setattr(periodic, "get_storage", lambda: storage)
    monkeypatch.setattr(periodic, "submissions_bucket", lambda: "subs-bucket")
    return storage, opened


async def test_one_bad_account_never_blocks_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    t = uuid.uuid4()
    bad, good = uuid.uuid4(), uuid.uuid4()
    storage, opened = _wire(monkeypatch, [(t, bad), (t, good)], {bad})
    counts = await periodic.purge_disconnected_accounts()
    assert counts == {"purged": 1, "failed": 1, "objects": 1}
    assert storage.deleted == [("subs-bucket", f"{t}/submissions/{good}/raw.jsonl")]
    assert opened == ["purger"]


async def test_nothing_due_opens_no_migrator_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _, opened = _wire(monkeypatch, [], set())
    assert await periodic.purge_disconnected_accounts() == {"purged": 0, "failed": 0, "objects": 0}
    assert opened == []


# ---- the maintenance run ----


async def test_run_goes_on_after_a_failed_step(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    disposed: list[bool] = []

    async def dispose() -> None:
        disposed.append(True)

    def step(name: str, fail: bool = False) -> Any:
        async def call() -> None:
            ran.append(name)
            if fail:
                raise RuntimeError(name)

        return call

    monkeypatch.setattr(maintenance, "dispose_engine", dispose)
    steps = (("a", step("a")), ("b", step("b", fail=True)), ("c", step("c")))
    assert await maintenance.run(steps) == 1
    assert ran == ["a", "b", "c"] and disposed == [True]
    assert await maintenance.run((("a", step("a")),)) == 0


def test_run_order_ends_with_the_purge() -> None:
    assert [name for name, _ in maintenance.STEPS] == [
        "oauth_states",
        "idempotency_keys",
        "retention",
        "account_purge",
    ]
    assert maintenance.STEPS[-1][1] is periodic.purge_disconnected_accounts
