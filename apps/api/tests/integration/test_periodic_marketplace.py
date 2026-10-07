"""S15 T16 (AC7, AC13; D8): the periodic marketplace token refresh and the OAuth state prune, as
`app_rw`, plus the one-shot entry point."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .jobs_support import reset_queue
from .marketplace_support import MktWorld, account_rows, connected_account, digest, mapi, mkt

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]


def _due(mw: MktWorld, account_id: str) -> None:
    mw.conn.execute(
        "UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute' WHERE id = %s",
        [account_id],
    )


def _refreshed(mw: MktWorld) -> dict[str, Any]:
    return {str(r["id"]): r["last_refresh_at"] for r in account_rows(mw.conn)}


async def test_periodic_refresh_selects_due_active_only(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    from app.workers.periodic import refresh_marketplace_tokens

    due = await connected_account(mapi, mkt, seller="A1DUE")
    _due(mkt, due["id"])
    later = await connected_account(mapi, mkt, marketplace="flipkart", seller="F1LATER")
    sick = await connected_account(mapi, mkt, "owner@bharat.test", seller="B1SICK")
    _due(mkt, sick["id"])
    mkt.conn.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now() "
        "WHERE id = %s",
        [sick["id"]],
    )
    counts = await refresh_marketplace_tokens()
    assert counts == {"refreshed": 1}
    refreshed = _refreshed(mkt)
    assert refreshed[due["id"]] is not None
    assert refreshed[later["id"]] is None and refreshed[sick["id"]] is None


async def test_periodic_refresh_skips_suspended_tenant(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    from app.workers.periodic import refresh_marketplace_tokens

    account = await connected_account(mapi, mkt)
    _due(mkt, account["id"])
    mkt.conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", [mkt.tenant("acme")])
    assert await refresh_marketplace_tokens() == {}
    assert mkt.control.refreshes == 0


async def test_periodic_refresh_two_tenants_written_per_tenant(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    from app.workers.periodic import refresh_marketplace_tokens

    acme = await connected_account(mapi, mkt, "owner@acme.test", seller="A1TWO")
    bharat = await connected_account(mapi, mkt, "owner@bharat.test", seller="B1TWO")
    for account in (acme, bharat):
        _due(mkt, account["id"])
    assert await refresh_marketplace_tokens() == {"refreshed": 2}
    rows = {str(r["id"]): r for r in account_rows(mkt.conn)}
    assert rows[acme["id"]]["tenant_id"] == mkt.tenant("acme")
    assert rows[bharat["id"]]["tenant_id"] == mkt.tenant("bharat")
    audits = mkt.conn.execute(
        "SELECT tenant_id, entity_id FROM audit_log WHERE action = "
        "'marketplace_account.credential_used'"
    ).fetchall()
    assert sorted((str(t), str(e)) for t, e in audits) == sorted(
        [(str(mkt.tenant("acme")), acme["id"]), (str(mkt.tenant("bharat")), bharat["id"])]
    )


async def test_periodic_refresh_one_failure_does_not_stop_the_pass(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import periodic

    one = await connected_account(mapi, mkt, "owner@acme.test", seller="A1ONE")
    two = await connected_account(mapi, mkt, "owner@bharat.test", seller="B1TWO")
    for account in (one, two):
        _due(mkt, account["id"])
    real = periodic.refresh_account  # type: ignore[attr-defined]
    calls: list[str] = []

    async def flaky(tenant_id: Any, account_id: Any) -> str:
        calls.append(str(account_id))
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await real(tenant_id, account_id)

    monkeypatch.setattr(periodic, "refresh_account", flaky)
    assert await periodic.refresh_marketplace_tokens() == {"error": 1, "refreshed": 1}
    assert len(calls) == 2


async def test_periodic_refresh_slot_fires_once_across_two_workers(
    mkt: MktWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S13's harness, on the real registration: two workers, one queued entry per slot (the
    `queueing_lock`), whatever the number of deferrers."""
    import asyncio

    from app.db.queue import new_queue_app
    from app.workers.periodic import PERIODIC, register_periodic
    from app.workers.tasks import register_tasks
    from procrastinate.periodic import PeriodicDeferrer

    reset_queue(mkt.conn)
    now = int(time.time())
    apps = []
    for _ in range(2):
        app = new_queue_app()
        register_tasks(app)
        register_periodic(app)
        apps.append(app)

    async def defer(app: Any) -> None:
        async with app.open_async():
            deferrer = PeriodicDeferrer(registry=app.periodic_registry)
            tasks = [
                (p, now)
                for p in app.periodic_registry.periodic_tasks.values()
                if p.periodic_id in ("marketplace_token_refresh", "oauth_states_prune")
            ]
            await deferrer.defer_jobs(tasks)

    await asyncio.gather(*(defer(app) for app in apps))
    rows = mkt.conn.execute(
        "SELECT task_name, args, queueing_lock FROM procrastinate_jobs ORDER BY task_name"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "periodic_marketplace_token_refresh",
        "periodic_oauth_states_prune",
    ]
    assert {r[2] for r in rows} == {"marketplace_token_refresh", "oauth_states_prune"}
    assert PERIODIC["marketplace_token_refresh"][0] == "*/5 * * * *"
    assert PERIODIC["oauth_states_prune"][0] == "30 * * * *"
    reset_queue(mkt.conn)


async def test_periodic_args_only_timestamp(mkt: MktWorld) -> None:
    from app.db.queue import new_queue_app
    from app.workers.periodic import register_periodic
    from app.workers.tasks import register_tasks
    from procrastinate.periodic import PeriodicDeferrer

    reset_queue(mkt.conn)
    app = new_queue_app()
    register_tasks(app)
    register_periodic(app)
    now = int(time.time())
    async with app.open_async():
        deferrer = PeriodicDeferrer(registry=app.periodic_registry)
        await deferrer.defer_jobs(
            [
                (p, now)
                for p in app.periodic_registry.periodic_tasks.values()
                if p.periodic_id == "marketplace_token_refresh"
            ]
        )
    [(args,)] = mkt.conn.execute("SELECT args FROM procrastinate_jobs").fetchall()
    assert args == {"timestamp": now}
    reset_queue(mkt.conn)


async def test_periodic_prune_oauth_states(mkt: MktWorld) -> None:
    from app.workers.periodic import prune_oauth_states

    def add(slug: str, label: str, *, pending: bool, expired: bool) -> None:
        extra = ", token_enc, kms_key_version, seller_id" if pending else ""
        values = ", %s, 'local-1', 'A1'" if pending else ""
        params: list[Any] = [
            mkt.tenant(slug),
            digest(label),
            mkt.user(f"owner@{slug}.test"),
            "-1 minute" if expired else "10 minutes",
        ]
        if pending:
            params.append(b"MKT1" + b"\x00" * 40)
        mkt.conn.execute(
            "INSERT INTO oauth_states (tenant_id, state_hash, user_id, marketplace, expires_at"  # noqa: S608
            + extra
            + ") VALUES (%s, %s, %s, 'amazon', now() + %s::interval"
            + values
            + ")",
            params,
        )

    for slug in ("acme", "bharat"):
        add(slug, f"{slug}-state-old", pending=False, expired=True)
        add(slug, f"{slug}-pending-old", pending=True, expired=True)
        add(slug, f"{slug}-state-live", pending=False, expired=False)
        add(slug, f"{slug}-pending-live", pending=True, expired=False)
    assert await prune_oauth_states() == 4
    left = {bytes(r[0]) for r in mkt.conn.execute("SELECT state_hash FROM oauth_states")}
    assert left == {
        digest(f"{slug}-{kind}-live")
        for slug in ("acme", "bharat")
        for kind in ("state", "pending")
    }


def test_oneshot_rejects_unknown_task(repo_root: Any) -> None:
    for args in ([], ["drop_everything"], ["refresh_marketplace_tokens", "--extra"]):
        result = subprocess.run(
            [sys.executable, "-m", "app.workers.oneshot", *args],
            cwd=repo_root / "apps/api",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 2, args
        assert "usage: python -m app.workers.oneshot" in result.stderr
