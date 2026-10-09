"""S20 T4 (AC14; D7; DATABASE.md:546-557): `python -m app.cli errors load`, the migrator-only,
versioned error-map loader, and `ErrorMapRepository.enabled_rows`."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app.cli import main

from .conftest import LOCAL_HOSTS, Conn, libpq_url

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
FIXTURE = Path(__file__).resolve().parents[2] / "app/db/seed_data/error_map.fake.json"


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


def entry(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "marketplace": "amazon",
        "match_on": "code",
        "raw_value": "FAKE-1",
        "version": 1,
        "error_code": "VALUE_NOT_ALLOWED",
        "error_class": "invalid_value",
        "retry_class": "user_action",
        "severity": "error",
        "auto_fixable": False,
        "fix_action": None,
        "field_hint": None,
        "plain_text": "{value} is not allowed for {field} on {marketplace}.",
        "seller_action": "Pick an allowed value.",
        "enabled": True,
    }
    row.update(over)
    return row


def write(tmp: Path, entries: Any, name: str = "m.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def rows(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT raw_value, version, enabled, error_code, plain_text FROM marketplace_error_map "
        "ORDER BY raw_value, version"
    ).fetchall()


def audits(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT tenant_id, actor_type, action, before, after FROM audit_log "
        "WHERE entity_type = 'marketplace_error_map' ORDER BY occurred_at, id"
    ).fetchall()


def run(path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["errors", "load", "--file", str(path)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_errors_load_idempotent(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    before = len(audits(superuser_conn))
    assert run(FIXTURE, capsys) == (0, "errors load: ok (5 inserted, 0 unchanged, 0 toggled)\n", "")
    first = rows(superuser_conn)
    assert len(first) == 5
    after_first = len(audits(superuser_conn))
    assert after_first == before + 5
    assert run(FIXTURE, capsys) == (0, "errors load: ok (0 inserted, 5 unchanged, 0 toggled)\n", "")
    assert rows(superuser_conn) == first
    assert len(audits(superuser_conn)) == after_first


def test_errors_load_enabled_toggle(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(write(cli_env, [entry()]), capsys)[0] == 0
    code, out, _ = run(write(cli_env, [entry(enabled=False)]), capsys)
    assert (code, out) == (0, "errors load: ok (0 inserted, 0 unchanged, 1 toggled)\n")
    assert [r[2] for r in rows(superuser_conn)] == [False]
    assert audits(superuser_conn)[-1][2:] == (
        "marketplace_error_map.enabled_changed",
        {"enabled": True},
        {"enabled": False},
    )


def test_errors_load_content_change_exit_1_no_rows(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(write(cli_env, [entry()]), capsys)[0] == 0
    changed = write(cli_env, [entry(raw_value="FAKE-2"), entry(plain_text="Other text.")])
    code, out, err = run(changed, capsys)
    assert (code, out) == (1, "")
    assert err == (
        "errors load: refused: amazon/code/FAKE-1 v1 already holds other content; bump version "
        "(nothing was written)\n"
    )
    assert [r[0] for r in rows(superuser_conn)] == ["FAKE-1"]  # FAKE-2 rolled back too
    bumped = write(cli_env, [entry(version=2, plain_text="Other text.")])
    assert run(bumped, capsys)[0] == 0
    assert [(r[0], r[1]) for r in rows(superuser_conn)] == [("FAKE-1", 1), ("FAKE-1", 2)]


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"error_code": "NOT_A_CODE"}, id="unknown code"),
        pytest.param({"field_hint": "content.nope"}, id="bad path"),
        pytest.param({"auto_fixable": True}, id="mem_fix mismatch"),
        pytest.param({"fix_action": "remap_value"}, id="fix_action without auto_fixable"),
        pytest.param({"plain_text": "{value.__class__} bad"}, id="bad placeholder"),
        pytest.param({"plain_text": "{0} bad"}, id="positional placeholder"),
        pytest.param({"plain_text": "x" * 501}, id="501-char text"),
        pytest.param({"enable": False}, id="unknown key"),
        pytest.param({"match_on": "message"}, id="bad match_on"),
        pytest.param({"retry_class": "sometimes"}, id="bad retry_class"),
    ],
)
def test_errors_load_invalid_row_exit_1_no_rows(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str], bad: dict[str, Any]
) -> None:
    path = write(cli_env, [entry(raw_value="FAKE-OK"), entry(**bad)])
    code, out, err = run(path, capsys)
    assert (code, out) == (1, "")
    assert err.startswith("errors load: invalid file (ValidationError)")
    assert rows(superuser_conn) == []


def test_errors_load_duplicate_key_in_file_refused(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(write(cli_env, [entry(), entry(enabled=False)]), capsys)
    assert code == 1 and "refused" in err
    assert rows(superuser_conn) == []


def test_errors_load_missing_file_exit_2(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(cli_env / "absent.json", capsys) == (2, "", "errors load: no such file\n")


def test_errors_load_not_json_exit_1(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cli_env / "bad.json"
    path.write_text("not json {", encoding="utf-8")
    code, _, err = run(path, capsys)
    assert code == 1 and err.startswith("errors load: invalid file")
    assert rows(superuser_conn) == []


def test_errors_load_audited_null_tenant(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(write(cli_env, [entry()]), capsys)[0] == 0
    (audit,) = audits(superuser_conn)
    assert audit == (None, "system", "marketplace_error_map.loaded", None, {"enabled": True})


def test_errors_load_audit_keys_enabled_only(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.repositories.audit import AUDITED_ENTITIES

    assert "marketplace_error_map" in AUDITED_ENTITIES
    keep = superuser_conn.execute("SELECT audit_allowlist('marketplace_error_map')").fetchone()
    assert keep == (["enabled"],)
    assert run(write(cli_env, [entry()]), capsys)[0] == 0
    assert run(write(cli_env, [entry(enabled=False)]), capsys)[0] == 0
    for _tenant, _actor, _action, before, after in audits(superuser_conn):
        assert set(after) == {"enabled"}
        assert before is None or set(before) == {"enabled"}


def test_error_map_repository_reads_enabled_only(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.repositories.error_map import ErrorMapRepository
    from mkos_schemas.codes import ErrorCode
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    entries = [
        entry(raw_value="FAKE-ON"),
        entry(raw_value="FAKE-OFF", enabled=False),
        entry(raw_value="FAKE-FK", marketplace="flipkart"),
    ]
    assert run(write(cli_env, entries), capsys)[0] == 0

    async def read() -> list[Any]:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with AsyncSession(engine) as session:
                return list(await ErrorMapRepository(session).enabled_rows("amazon"))
        finally:
            await engine.dispose()

    got = asyncio.run(read())
    assert [(r.raw_value, r.error_code) for r in got] == [("FAKE-ON", ErrorCode.VALUE_NOT_ALLOWED)]


def test_app_rw_cannot_write_error_map(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.support.dsn import as_app_rw

    assert run(write(cli_env, [entry()]), capsys)[0] == 0
    url = libpq_url(as_app_rw(os.environ["DATABASE_URL"]))
    with psycopg.connect(url, autocommit=True) as app_rw:
        for sql in (
            "UPDATE marketplace_error_map SET enabled = false",
            "DELETE FROM marketplace_error_map",
            "INSERT INTO marketplace_error_map (marketplace, match_on, raw_value, version, "
            "error_code, error_class, retry_class, severity, auto_fixable, plain_text, enabled) "
            "VALUES ('amazon', 'code', 'X', 1, 'VALUE_NOT_ALLOWED', 'invalid_value', "
            "'user_action', 'error', false, 't', true)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_rw.execute(sql)
        assert app_rw.execute("SELECT count(*) FROM marketplace_error_map").fetchone() == (1,)
