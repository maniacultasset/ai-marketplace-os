"""SI13 T16 (AC16): `errors export` from the committed seed: the markdown table and a CSV that
parses, with no tenant id, raw message or unapproved proposal text."""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.cli import main

from .conftest import LOCAL_HOSTS, Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
DRAFT = "A draft cause nobody approved yet for this code."


@pytest.fixture
def loaded(
    clean_tenant_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, superuser_conn: Conn
) -> Iterator[None]:
    from app.core.config import get_settings
    from app.db.session import get_engine, get_sessionmaker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    get_settings.cache_clear()
    # `errors export` reads through the cached app engine: one an earlier test in this worker built
    # for another database (the unit suites' CI DATABASE_URL, the template with no error map) would
    # read an empty map (CI shard 1, 2026-11-09)
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
    assert main(["errors", "load"]) == 0
    superuser_conn.execute("SET SESSION AUTHORIZATION migrator")
    try:
        superuser_conn.execute(
            "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status, "
            "plain_cause, suggested_fix, lane_class) VALUES ('amazon', 'code', '99999', "
            "'proposed', %s, %s, 'seller')",
            [DRAFT, DRAFT],
        )
    finally:
        superuser_conn.execute("RESET SESSION AUTHORIZATION")
    yield
    get_settings.cache_clear()


def test_markdown_and_csv(loaded: None, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert main(["errors", "export"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("| Marketplace | Code | Who fixes it |")
    assert "| amazon | 4005007 | Marketplace |" in md
    assert "| amazon | 4005013 | Kuinn team |" in md
    assert "Codes seen with no row yet:" in md
    assert main(["errors", "export", "--format", "csv"]) == 0
    out = capsys.readouterr().out
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[0][2] == "Who fixes it" and len(parsed) > 10
    for text in (md, out):
        assert DRAFT not in text and "tenant" not in text.casefold()
