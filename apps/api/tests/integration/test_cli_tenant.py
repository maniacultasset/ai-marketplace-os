"""S16 T19-T20 (DW4, AC8; CP9): `tenant create` and `tenant add-member`, as migrator on the admin
URL; the people they create log in through S10."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .cli_support import PASSWORD, cli, create_argv, login, staff_audit
from .conftest import LOCAL_HOSTS
from .jobs_support import count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
Conn = Any


async def test_cli_create_owner(
    auth_db: Conn, api: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """DW4: a tenant, its owner, the membership and two staff audit rows; the owner logs in and
    reads the tenant."""
    code, out, err = await cli(create_argv("new-co", "Owner@New.test"), capsys, PASSWORD + "\n")
    assert (code, err) == (0, "")
    line = out.strip().splitlines()[-1]  # the first import of app.cli may log alembic lines
    tenant_id = line.split()[0].split("=", 1)[1]
    assert line == f"tenant_id={tenant_id} slug=new-co"
    assert auth_db.execute(
        "SELECT status, trial_ends_at FROM tenants WHERE slug = 'new-co'"
    ).fetchone() == ("active", None)
    created = staff_audit(auth_db, "tenant.created")
    member = staff_audit(auth_db, "membership.created")
    assert [(r[0], r[1], str(r[2]), r[4]) for r in created] == [
        ("staff", "staff@ops.test", tenant_id, {"name": "Tenant new-co"})
    ]
    assert [(r[0], r[3], r[4]) for r in member] == [
        ("staff", "membership", {"role": "owner", "is_active": True})
    ]
    assert created[0][5] == member[0][5] and created[0][5]
    token = (await login(api, "owner@new.test")).json()
    response = await api.get(
        "/api/v1/tenants/me", headers={"Authorization": f"Bearer {token['access_token']}"}
    )
    assert response.status_code == 200 and response.json()["slug"] == "new-co"


async def test_cli_create_reuses_existing_user(
    auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    from .auth_support import add_user

    add_user(auth_db, "kept@user.test")
    before = auth_db.execute(
        "SELECT hashed_password FROM users WHERE email = 'kept@user.test'"
    ).fetchone()
    code, _, _ = await cli(create_argv("kept-co", "kept@user.test"), capsys, "")
    assert code == 0
    after = auth_db.execute(
        "SELECT hashed_password FROM users WHERE email = 'kept@user.test'"
    ).fetchone()
    assert before == after


async def test_cli_create_inactive_user_exit_4(
    auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    from .auth_support import add_user

    add_user(auth_db, "gone@user.test")
    auth_db.execute("UPDATE users SET is_active = false WHERE email = 'gone@user.test'")
    code, _, _ = await cli(create_argv("gone-co", "gone@user.test"), capsys, "")
    assert code == 4 and count(auth_db, "SELECT count(*) FROM tenants WHERE slug = 'gone-co'") == 0


async def test_cli_create_trial(auth_db: Conn, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = await cli(
        create_argv("trial-co", "t@trial.test", "--trial-days", "14"), capsys, PASSWORD + "\n"
    )
    assert code == 0
    row = auth_db.execute(
        "SELECT status, trial_ends_at > now() + interval '13 days' FROM tenants WHERE slug = 'trial-co'"
    ).fetchone()
    assert row == ("trial", True)


async def test_cli_duplicate_slug_exit_4_nothing_written(
    auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (await cli(create_argv("dup-co", "a@dup.test"), capsys, PASSWORD + "\n"))[0] == 0
    users = count(auth_db, "SELECT count(*) FROM users")
    code, out, err = await cli(create_argv("dup-co", "b@dup.test"), capsys, PASSWORD + "\n")
    assert code == 4 and "tenant_id=" not in out and "slug is taken" in err
    assert count(auth_db, "SELECT count(*) FROM users") == users
    assert len(staff_audit(auth_db, "tenant.created")) == 1


async def test_cli_create_failure_rolls_back_all(
    auth_db: Conn, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import staff_ops

    real = staff_ops._member

    async def boom(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        raise RuntimeError("audit down")

    monkeypatch.setattr(staff_ops, "_member", boom)
    code, _, err = await cli(create_argv("boom-co", "x@boom.test"), capsys, PASSWORD + "\n")
    assert code == 1 and "RuntimeError" in err
    assert count(auth_db, "SELECT count(*) FROM tenants WHERE slug = 'boom-co'") == 0
    assert count(auth_db, "SELECT count(*) FROM users WHERE email = 'x@boom.test'") == 0
    assert staff_audit(auth_db, "tenant.created") == []


async def test_cli_bad_password_exit_2(auth_db: Conn, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = await cli(create_argv("pw-co", "p@pw.test"), capsys, "short\n")
    assert code == 2 and "short" not in err
    assert count(auth_db, "SELECT count(*) FROM tenants WHERE slug = 'pw-co'") == 0


async def test_cli_output_has_no_secrets(auth_db: Conn, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    code, out, err = await cli(create_argv("quiet-co", "q@quiet.test"), capsys, PASSWORD + "\n")
    (hashed,) = auth_db.execute(
        "SELECT hashed_password FROM users WHERE email = 'q@quiet.test'"
    ).fetchone()
    text = out + err
    assert code == 0 and PASSWORD not in text and hashed not in text
    assert os.environ["ALEMBIC_DATABASE_URL"] not in text and "postgres:" not in text


ADD = [
    "tenant",
    "add-member",
    "--slug",
    "acme",
    "--email",
    "second@acme.test",
    "--role",
    "reviewer",
    "--staff-email",
    "staff@ops.test",
]


async def test_cli_add_member(
    products_world: Any, auth_db: Conn, api: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = await cli(ADD, capsys, PASSWORD + "\n")
    assert code == 0
    assert out.strip().splitlines()[-1].startswith(f"tenant_id={products_world.tenant('acme')} ")
    body = (await login(api, "second@acme.test")).json()
    assert body["memberships"][0]["role"] == "reviewer"
    assert [r[0] for r in staff_audit(auth_db, "membership.created")] == ["staff"]


async def test_cli_add_member_existing_exit_4(
    products_world: Any, auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = list(ADD)
    argv[argv.index("--email") + 1] = "owner@acme.test"
    assert (await cli(argv, capsys, ""))[0] == 4
    assert staff_audit(auth_db, "membership.created") == []


async def test_cli_add_member_unknown_slug_exit_4(
    products_world: Any, auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = list(ADD)
    argv[argv.index("--slug") + 1] = "nope"
    assert (await cli(argv, capsys, PASSWORD + "\n"))[0] == 4


async def test_cli_add_member_suspended_tenant_exit_4(
    products_world: Any, auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    auth_db.execute("UPDATE tenants SET status = 'suspended' WHERE slug = 'acme'")
    assert (await cli(ADD, capsys, PASSWORD + "\n"))[0] == 4
    assert count(auth_db, "SELECT count(*) FROM users WHERE email = 'second@acme.test'") == 0
