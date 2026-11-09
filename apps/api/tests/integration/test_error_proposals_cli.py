"""SI13 T14 (AC14; P:B3, D6; CP-D4, CP-D5, CP-D17, CP-D20): `errors proposals list|approve|reject`.
Only an active platform admin approves; only a `proposed` row; the final row passes the full
diagnosis check again (a planted row never reaches the map); the seed JSON names no email."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.cli import ERROR_MAP_AMAZON_PATH, main

from . import factories as f
from .conftest import LOCAL_HOSTS, Conn
from .integrity_support import as_app_rw, scalar

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

ADMIN = "admin@platform.test"
CAUSE = "Amazon refused a value it expects in another format for this product."
FIX = "Check the product details Amazon names, correct them and send the listing again."


@pytest.fixture
def cli_env(
    clean_tenant_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _proposed(conn: Conn, code: str = "77777", **over: Any) -> uuid.UUID:
    pid = scalar(
        conn,
        "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status) "
        "VALUES ('amazon', 'code', %s, 'pending') RETURNING id",
        [code],
    )
    values = {
        "plain_cause": CAUSE,
        "suggested_fix": FIX,
        "lane_class": "seller",
        "error_code": "VALUE_NOT_ALLOWED",
        "error_class": "invalid_value",
        **over,
    }
    sets = ", ".join(f"{k} = %s" for k in values)
    with as_app_rw(conn):
        conn.execute(
            f"UPDATE marketplace_error_proposals SET status = 'proposed', {sets} WHERE id = %s",  # noqa: S608
            [*values.values(), pid],
        )
    return uuid.UUID(str(pid))


def _admin(conn: Conn, **over: Any) -> uuid.UUID:
    return uuid.UUID(
        str(f.user(conn, email=over.pop("email", ADMIN), is_platform_admin=True, **over))
    )


def _approve(pid: uuid.UUID, by: str = ADMIN, *extra: str) -> int:
    return main(["errors", "proposals", "approve", str(pid), "--by", by, *extra])


def _status(conn: Conn, pid: uuid.UUID) -> str:
    return str(scalar(conn, "SELECT status FROM marketplace_error_proposals WHERE id = %s", [pid]))


def _maps(conn: Conn) -> int:
    return int(scalar(conn, "SELECT count(*) FROM marketplace_error_map WHERE raw_value = '77777'"))


# ---- the happy path ----


def test_approve_writes_one_row_and_prints_the_seed(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _admin(superuser_conn)
    pid = _proposed(superuser_conn)
    assert _approve(pid) == 0
    out = capsys.readouterr().out
    seed = json.loads(out)
    assert seed["version"] == 1 and seed["raw_value"] == "77777"
    assert seed["policy_ref"] == {
        "source": "staff-approved-ai-proposal",
        "proposal_id": str(pid),
        "approved_by": str(admin),
        "observed": False,
    }
    assert "@" not in out and ADMIN not in out
    assert seed["retry_class"] == "user_action" and seed["fix_action"] is None
    assert _maps(superuser_conn) == 1 and _status(superuser_conn, pid) == "approved"
    audit = superuser_conn.execute(
        "SELECT tenant_id, after FROM audit_log WHERE action = 'marketplace_error_proposal.approved'"
    ).fetchall()
    assert audit == [(None, {"status": "approved"})]
    # the printed row loads as unchanged once committed to the seed
    copy = cli_env / "map.json"
    rows = json.loads(ERROR_MAP_AMAZON_PATH.read_text("utf-8"))
    copy.write_text(json.dumps([seed]), "utf-8")
    assert main(["errors", "load", "--file", str(copy)]) == 0
    assert "(0 inserted, 1 unchanged, 0 toggled)" in capsys.readouterr().out
    # the widened seed test accepts exactly this source
    from app.db.platform_loads import load_error_map_file
    from tests.unit.test_error_map_amazon import assert_documented

    copy.write_text(json.dumps([*rows, seed]), "utf-8")
    for row in load_error_map_file(copy):
        assert_documented(row)
    bad = {**seed, "policy_ref": {**seed["policy_ref"], "source": "other"}}
    copy.write_text(json.dumps([bad]), "utf-8")
    (row,) = load_error_map_file(copy)
    with pytest.raises(AssertionError):
        assert_documented(row)


def test_approve_twice_is_one_row(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    _admin(superuser_conn)
    pid = _proposed(superuser_conn)
    assert _approve(pid) == 0
    capsys.readouterr()
    assert _approve(pid) == 0
    assert capsys.readouterr().out == "errors proposals: already approved\n"
    assert _maps(superuser_conn) == 1


# ---- refusals ----


@pytest.mark.parametrize("who", ["non_admin", "inactive", "unknown"])
def test_only_an_active_platform_admin(cli_env: Path, superuser_conn: Conn, who: str) -> None:
    if who == "non_admin":
        f.user(superuser_conn, email=ADMIN)
    elif who == "inactive":
        _admin(superuser_conn, is_active=False)
    pid = _proposed(superuser_conn)
    assert _approve(pid) == 2
    assert _maps(superuser_conn) == 0 and _status(superuser_conn, pid) == "proposed"


def test_a_failed_row_cannot_be_approved(cli_env: Path, superuser_conn: Conn) -> None:
    _admin(superuser_conn)
    pid = scalar(
        superuser_conn,
        "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status) "
        "VALUES ('amazon', 'code', '77777', 'pending') RETURNING id",
    )
    superuser_conn.execute(
        "UPDATE marketplace_error_proposals SET status = 'failed', failure_reason = 'budget' "
        "WHERE id = %s",
        [pid],
    )
    assert _approve(uuid.UUID(str(pid))) == 2 and _maps(superuser_conn) == 0


def test_an_override_with_a_url_is_refused(cli_env: Path, superuser_conn: Conn) -> None:
    _admin(superuser_conn)
    pid = _proposed(superuser_conn)
    assert _approve(pid, ADMIN, "--plain-text", "See https://x.example for the fix here.") == 2
    assert _maps(superuser_conn) == 0


@pytest.mark.parametrize(
    "planted",
    [
        {"plain_cause": "Amazon wants https://evil.example/fix here now."},
        {"error_code": "NOT_A_CODE"},
        {"field_hint": "attributes.not_in_the_dictionary_x"},
    ],
)
def test_a_planted_row_never_reaches_the_map(
    cli_env: Path, superuser_conn: Conn, planted: dict[str, str]
) -> None:
    _admin(superuser_conn)
    pid = _proposed(superuser_conn, **planted)
    assert _approve(pid) == 2
    assert _maps(superuser_conn) == 0 and _status(superuser_conn, pid) == "proposed"


# ---- reject and list ----


def test_reject_stores_the_note_and_list_has_no_tenant_ids(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _admin(superuser_conn)
    pid = _proposed(superuser_conn)
    other = _proposed(superuser_conn, code="88888")
    note = "Not a real cause; we saw it once."
    assert main(["errors", "proposals", "reject", str(pid), "--by", ADMIN, "--note", note]) == 0
    row = superuser_conn.execute(
        "SELECT status, review_note, reviewed_by FROM marketplace_error_proposals WHERE id = %s",
        [pid],
    ).fetchone()
    assert row == ("rejected", note, admin)
    capsys.readouterr()
    assert main(["errors", "proposals", "list", "--status", "proposed"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["id"] for x in lines] == [str(other)]
    for line in lines:
        assert not {"tenant_id", "sample_issue_id", "agent_run_id", "job_id"} & set(line)
