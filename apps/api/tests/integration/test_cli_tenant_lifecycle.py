"""SI5 T9 (AC13): `tenant activate` and `tenant extend-trial` as migrator on the admin URL, through
the same 0017 functions a platform admin calls over HTTP. Exit 0 with a staff audit row; exit 2 for
bad arguments; exit 4 (`EXIT_NOT_ELIGIBLE`) for an unknown slug or a refused move."""

from __future__ import annotations

from typing import Any

import pytest

from .auth_support import add_tenant
from .cli_support import cli, staff_audit
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = Any
STAFF = ["--staff-email", "staff@ops.test"]


def _activate(slug: str) -> list[str]:
    return ["tenant", "activate", "--slug", slug, "--reason", "paid by bank transfer", *STAFF]


def _extend(slug: str, days: str = "30") -> list[str]:
    return ["tenant", "extend-trial", "--slug", slug, "--days", days, "--reason", "pilot", *STAFF]


def _expire(conn: Conn, slug: str) -> None:
    conn.execute(
        "UPDATE tenants SET trial_ends_at = now() - interval '1 day' WHERE slug = %s", [slug]
    )
    conn.execute("SET SESSION AUTHORIZATION migrator")
    try:
        conn.execute("SELECT expire_trials()")
    finally:
        conn.execute("RESET SESSION AUTHORIZATION")


async def test_cli_activate_a_suspended_trial(
    auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    add_tenant(auth_db, "late-co", status="trial")
    _expire(auth_db, "late-co")
    code, out, err = await cli(_activate("late-co"), capsys)
    assert (code, err) == (0, ""), err
    assert out.strip().splitlines()[-1].endswith("outcome=activated")
    assert auth_db.execute("SELECT status FROM tenants WHERE slug = 'late-co'").fetchone() == (
        "active",
    )
    rows = staff_audit(auth_db, "tenant.active")
    assert [(r[0], r[1], r[3]) for r in rows] == [("staff", "staff@ops.test", "tenant")]
    again, out, _ = await cli(_activate("late-co"), capsys)
    assert again == 0 and out.strip().endswith("outcome=unchanged")


async def test_cli_extend_a_trial_expired_tenant(
    auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    add_tenant(auth_db, "slow-co", status="trial")
    _expire(auth_db, "slow-co")
    code, out, err = await cli(_extend("slow-co", "14"), capsys)
    assert (code, err) == (0, ""), err
    assert out.strip().endswith("outcome=extended")
    row = auth_db.execute(
        "SELECT status, abs(extract(epoch FROM trial_ends_at - (now() + interval '14 days'))) < 60 "
        "FROM tenants WHERE slug = 'slow-co'"
    ).fetchone()
    assert row == ("trial", True)
    assert len(staff_audit(auth_db, "tenant.trial_extended")) == 1


@pytest.mark.parametrize("move", ["activate", "extend"])
async def test_cli_refusals_exit_4(
    auth_db: Conn, capsys: pytest.CaptureFixture[str], move: str
) -> None:
    add_tenant(auth_db, "gone-co", status="offboarded")
    argv = _activate if move == "activate" else _extend
    for slug in ("gone-co", "no-such-co"):
        code, _, err = await cli(argv(slug), capsys)
        assert code == 4, (slug, err)
        assert "refused" in err


async def test_cli_extend_a_staff_suspended_tenant_exits_4(
    auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant = add_tenant(auth_db, "held-co")
    auth_db.execute("SET SESSION AUTHORIZATION migrator")
    try:
        auth_db.execute("SELECT set_tenant_lifecycle(%s, 'suspended', 'unpaid')", [tenant])
    finally:
        auth_db.execute("RESET SESSION AUTHORIZATION")
    code, _, err = await cli(_extend("held-co"), capsys)
    assert code == 4, err
    assert auth_db.execute("SELECT status FROM tenants WHERE id = %s", [tenant]).fetchone() == (
        "suspended",
    )


def test_cli_days_zero_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import main

    with pytest.raises(SystemExit) as info:
        main(_extend("any-co", "0"))
    assert info.value.code == 2
    capsys.readouterr()
