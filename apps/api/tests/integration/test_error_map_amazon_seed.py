"""SI9 T3 (AC1): `errors load` with no `--file` loads the committed Amazon error map into a fresh
database, idempotently, with one audit row per inserted row; an edited copy at the same version
is refused whole."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.cli import ERROR_MAP_AMAZON_PATH, main

from .conftest import LOCAL_HOSTS, Conn
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


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


def _amazon_rows(conn: Conn) -> int:
    return int(
        scalar(
            conn,
            "SELECT count(*) FROM marketplace_error_map WHERE marketplace = 'amazon' "
            "AND raw_value NOT LIKE 'FAKE%%'",
        )
    )


def _audits(conn: Conn) -> int:
    return int(
        scalar(conn, "SELECT count(*) FROM audit_log WHERE entity_type = 'marketplace_error_map'")
    )


def test_default_file_loads_idempotently(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    n = len(json.loads(ERROR_MAP_AMAZON_PATH.read_text(encoding="utf-8")))
    before = _audits(superuser_conn)
    assert main(["errors", "load"]) == 0
    assert capsys.readouterr().out == f"errors load: ok ({n} inserted, 0 unchanged, 0 toggled)\n"
    assert _amazon_rows(superuser_conn) == n
    assert _audits(superuser_conn) == before + n
    assert main(["errors", "load"]) == 0
    assert capsys.readouterr().out == f"errors load: ok (0 inserted, {n} unchanged, 0 toggled)\n"
    assert _audits(superuser_conn) == before + n


def test_changed_text_same_version_refused(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["errors", "load"]) == 0
    capsys.readouterr()
    stored = _amazon_rows(superuser_conn)
    audits = _audits(superuser_conn)
    entries = json.loads(ERROR_MAP_AMAZON_PATH.read_text(encoding="utf-8"))
    entries[0]["plain_text"] = "Changed text."
    copy = cli_env / "amazon.json"
    copy.write_text(json.dumps(entries), encoding="utf-8")
    assert main(["errors", "load", "--file", str(copy)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "already holds other content; bump version" in out.err
    assert _amazon_rows(superuser_conn) == stored
    assert _audits(superuser_conn) == audits
