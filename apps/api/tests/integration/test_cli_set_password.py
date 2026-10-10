"""S16 T20a (AC8; PLAN D6): `user set-password` sets a new argon2id hash and revokes every live
refresh token of the user in each of their tenants, in one audited transaction."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from .cli_support import PASSWORD, cli, login, staff_audit
from .conftest import LOCAL_HOSTS
from .jobs_support import count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
NEW = "a-brand-new-password-9"
SET = ["user", "set-password", "--email", "owner@acme.test", "--staff-email", "staff@ops.test"]


@pytest.fixture
def world_with_password(products_world: Any, auth_db: Any) -> Any:
    from app.core.passwords import hash_password

    auth_db.execute(
        "UPDATE users SET hashed_password = %s WHERE email = 'owner@acme.test'",
        [hash_password(PASSWORD)],
    )
    return products_world


async def test_cli_set_password_revokes_refresh_families(
    world_with_password: Any,
    auth_db: Any,
    api: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = (await login(api, "owner@acme.test")).json()["refresh_token"]
    second = (await login(api, "owner@acme.test")).json()["refresh_token"]
    code, out, _ = await cli(SET, capsys, NEW + "\n")
    assert code == 0 and out.strip().endswith("revoked=2")
    for token in (first, second):
        response = await api.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401
    assert (await login(api, "owner@acme.test", NEW)).status_code == 200
    assert (await login(api, "owner@acme.test")).status_code == 401
    reasons = auth_db.execute(
        "SELECT DISTINCT revoked_reason FROM refresh_tokens WHERE revoked_at IS NOT NULL AND replaced_by IS NULL"
    ).fetchall()
    assert ("password_changed",) in reasons


async def test_cli_set_password_audited(
    world_with_password: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from .auth_support import add_member

    world = world_with_password
    add_member(auth_db, world.tenant("bharat"), world.user("owner@acme.test"), "reviewer")
    assert (await cli(SET, capsys, NEW + "\n"))[0] == 0
    rows = staff_audit(auth_db, "user.password_set")
    assert sorted(str(r[2]) for r in rows) == sorted(
        [str(world.tenant("acme")), str(world.tenant("bharat"))]
    )
    assert all(r[0] == "staff" and r[3] == "user" and r[4] in (None, {}) for r in rows)
    (hashed,) = auth_db.execute(
        "SELECT hashed_password FROM users WHERE email = 'owner@acme.test'"
    ).fetchone()
    assert hashed not in repr(rows)


async def test_cli_set_password_unknown_email_refused(
    world_with_password: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = list(SET)
    argv[argv.index("--email") + 1] = "nobody@acme.test"
    assert (await cli(argv, capsys, NEW + "\n"))[0] == 4
    assert staff_audit(auth_db, "user.password_set") == []


async def test_cli_set_password_failure_rolls_back(
    world_with_password: Any,
    auth_db: Any,
    api: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import staff_ops

    await login(api, "owner@acme.test")

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(staff_ops, "_audit", boom)
    before = auth_db.execute(
        "SELECT hashed_password FROM users WHERE email = 'owner@acme.test'"
    ).fetchone()
    assert (await cli(SET, capsys, NEW + "\n"))[0] == 1
    assert (
        auth_db.execute(
            "SELECT hashed_password FROM users WHERE email = 'owner@acme.test'"
        ).fetchone()
        == before
    )
    assert (
        count(
            auth_db, "SELECT count(*) FROM refresh_tokens WHERE revoked_reason = 'password_changed'"
        )
        == 0
    )


async def test_cli_set_password_output_has_no_secrets(
    world_with_password: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = await cli(SET, capsys, NEW + "\n")
    (hashed,) = auth_db.execute(
        "SELECT hashed_password FROM users WHERE email = 'owner@acme.test'"
    ).fetchone()
    assert code == 0 and NEW not in out + err and hashed not in out + err


async def _until_a_lock_wait(auth_db: Any) -> None:
    """Yield to the event loop until some backend of this database waits on a row lock."""
    for _ in range(200):
        (waiting,) = auth_db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND datname = current_database()"
        ).fetchone()
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("set-password never waited on the in-flight refresh")


async def test_cli_set_password_revokes_a_token_rotated_concurrently(
    world_with_password: Any,
    auth_db: Any,
    api: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-3: a refresh in flight while staff reset the password (the rotation's successor is
    inserted but not committed when the revoke runs) must not leave that successor live."""
    from app.repositories.identity import RefreshTokenRepository

    stolen = (await login(api, "owner@acme.test")).json()["refresh_token"]
    rotated, release = asyncio.Event(), asyncio.Event()
    original = RefreshTokenRepository.rotate

    async def held_rotate(self: Any, *args: Any, **kwargs: Any) -> None:
        await original(self, *args, **kwargs)
        rotated.set()
        await release.wait()  # still inside the refresh transaction

    monkeypatch.setattr(RefreshTokenRepository, "rotate", held_rotate)
    refreshing = asyncio.create_task(
        api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    )
    await rotated.wait()
    resetting = asyncio.create_task(cli(SET, capsys, NEW + "\n"))
    try:
        await _until_a_lock_wait(auth_db)
    finally:
        release.set()
    successor = (await refreshing).json()["refresh_token"]
    code, out, _ = await resetting
    monkeypatch.setattr(RefreshTokenRepository, "rotate", original)

    assert code == 0 and out.strip().endswith("revoked=1")
    assert (
        count(
            auth_db,
            "SELECT count(*) FROM refresh_tokens rt JOIN users u ON u.id = rt.user_id "
            "WHERE u.email = 'owner@acme.test' AND rt.revoked_at IS NULL",
        )
        == 0
    )
    after = await api.post("/api/v1/auth/refresh", json={"refresh_token": successor})
    assert after.status_code == 401, after.text
