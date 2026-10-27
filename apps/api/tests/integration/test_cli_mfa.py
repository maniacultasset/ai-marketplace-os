"""DPP-1 AC20 (ADR-0021): the staff CLI's `user unlock` and `user reset-mfa`, and the DPP rules on
every password staff set (each one audited migrator transaction)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .cli_support import PASSWORD, cli, login, staff_audit
from .conftest import LOCAL_HOSTS
from .mfa_support import count, lock, lock_state, user_id

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"
UNLOCK = ["user", "unlock", "--email", EMAIL, "--staff-email", "staff@ops.test"]
RESET = ["user", "reset-mfa", "--email", EMAIL, "--staff-email", "staff@ops.test"]
SET = ["user", "set-password", "--email", EMAIL, "--staff-email", "staff@ops.test"]
FRESH = "Brass-Harbour-Path-4400"


@pytest.fixture
def world(products_world: Any, auth_db: Any) -> Any:
    from app.core.passwords import hash_password

    auth_db.execute(
        "UPDATE users SET hashed_password = %s WHERE email = %s", [hash_password(PASSWORD), EMAIL]
    )
    return products_world


def _seed_mfa(conn: Any, uid: Any) -> None:
    import hashlib

    conn.execute(
        "INSERT INTO user_mfa_factors (user_id, status, secret_blob, key_version, confirmed_at) "
        "VALUES (%s, 'active', 'MFA1-x', 'local-1', now())",
        [uid],
    )
    conn.execute(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
        [uid, hashlib.sha256(b"rc").digest()],
    )


async def test_unlock_clears_and_audits(
    world: Any, auth_db: Any, api: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    lock(auth_db, EMAIL)
    assert (await login(api, EMAIL)).status_code == 401
    code, out, _ = await cli(UNLOCK, capsys)
    assert code == 0 and out.strip().endswith("unlocked=true")
    assert lock_state(auth_db, EMAIL) == (0, False)
    [row] = staff_audit(auth_db, "auth.unlocked")
    assert row[0] == "staff" and row[1] == "staff@ops.test" and row[2] is None
    assert (await login(api, EMAIL)).status_code == 200


async def test_reset_mfa_deletes_factor_codes_revokes_families_audits(
    world: Any, auth_db: Any, api: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = user_id(auth_db, EMAIL)
    session = (await login(api, EMAIL)).json()
    _seed_mfa(auth_db, uid)
    code, out, _ = await cli(RESET, capsys, FRESH + "\n")
    assert code == 0 and "mfa_reset=true" in out
    assert count(auth_db, "SELECT count(*) FROM user_mfa_factors WHERE user_id = %s", [uid]) == 0
    assert count(auth_db, "SELECT count(*) FROM user_recovery_codes WHERE user_id = %s", [uid]) == 0
    refreshed = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    assert refreshed.status_code == 401
    [row] = staff_audit(auth_db, "auth.mfa_reset")
    assert row[0] == "staff" and row[2] is None
    assert (await login(api, EMAIL, FRESH)).status_code == 200


async def test_reset_mfa_sets_must_change(
    world: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (await cli(RESET, capsys, FRESH + "\n"))[0] == 0
    flags = auth_db.execute(
        "SELECT password_must_change, mfa_last_changed_at IS NOT NULL FROM users WHERE email = %s",
        [EMAIL],
    ).fetchone()
    assert flags == (True, True)


async def test_reset_mfa_refuses_weak_password(
    world: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = user_id(auth_db, EMAIL)
    _seed_mfa(auth_db, uid)
    code, _, err = await cli(RESET, capsys, "owner-weak-password\n")
    assert code == 2 and "owner-weak-password" not in err
    assert count(auth_db, "SELECT count(*) FROM user_mfa_factors WHERE user_id = %s", [uid]) == 1
    assert staff_audit(auth_db, "auth.mfa_reset") == []


async def test_set_password_history_and_policy(
    world: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    weak = await cli(SET, capsys, "alllowercase-pass\n")
    assert weak[0] == 2 and "upper-case" in weak[2]
    assert (await cli(SET, capsys, FRESH + "\n"))[0] == 0
    reused = await cli(SET, capsys, FRESH + "\n")
    assert reused[0] == 2 and "last 10" in reused[2]
    again = await cli(SET, capsys, PASSWORD + "\n")  # the one before, still in the history
    assert again[0] == 2
    uid = user_id(auth_db, EMAIL)
    assert (
        count(auth_db, "SELECT count(*) FROM user_password_history WHERE user_id = %s", [uid]) == 1
    )


async def test_unknown_email_exit_code(
    world: Any, auth_db: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (UNLOCK, RESET):
        changed = list(argv)
        changed[changed.index("--email") + 1] = "nobody@acme.test"
        stdin = FRESH + "\n" if argv is RESET else ""
        assert (await cli(changed, capsys, stdin))[0] == 4
