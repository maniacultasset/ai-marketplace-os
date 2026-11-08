"""DW2 T9 (AC6, AC7, AC12): `templates refresh | promote-definition | diff` end to end, against the
in-process fake marketplace (the CLI's client factory is swapped; no network) and mos_test."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from app import cli
from app.adapters.amazon.definitions import DefinitionsClient, LinkPolicy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from tests.adapters.amazon_support import FakeTokens, FixedRng, spapi_client
from tests.support.fake_marketplace import build_fake, ptd_fixture

from .conftest import LOCAL_HOSTS, Conn

pytestmark = [pytest.mark.allow_hosts(LOCAL_HOSTS), pytest.mark.usefixtures("clean_tenant_db")]

STAFF = ["--staff-email", "Staff@Example.com", "--reason", "DW2 refresh test"]
PLATFORM_TOKEN = "Atzr|Zq9-platform-refresh"


class Fake:
    def __init__(self) -> None:
        self.app = build_fake()
        self.tokens = FakeTokens(self.app)

    def factory(self, settings: Any) -> DefinitionsClient:
        async def resolve(host: str) -> Sequence[str]:
            return ["52.95.150.10"]

        sp = spapi_client(self.app, seller_id=None, tokens=self.tokens, rng=FixedRng(0.0))
        return DefinitionsClient(
            sp,
            marketplace_id=settings.amazon_marketplace_id,
            link_policy=LinkPolicy.from_setting(settings.amazon_ptd_link_hosts),
            transport=httpx.ASGITransport(self.app),
            resolver=resolve,
        )

    def control(self, **ptd: Any) -> None:
        state = self.app.state.fake
        for name, value in ptd.items():
            state.ptd[name] = value


@pytest.fixture
def fake(db_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    monkeypatch.setenv("AMAZON_CLIENT_ID", "amzn1.application-oa2-client.REPLACE_ME")
    monkeypatch.setenv("AMAZON_CLIENT_SECRET", "REPLACE_ME")
    monkeypatch.setenv("AMAZON_PLATFORM_REFRESH_TOKEN", PLATFORM_TOKEN)
    monkeypatch.setenv("AMAZON_SPAPI_ENDPOINT", "https://sandbox.sellingpartnerapi-eu.amazon.com")
    monkeypatch.setenv("APP_ROLE", "shell")
    get_settings.cache_clear()
    created = Fake()
    monkeypatch.setattr(cli, "DEFINITIONS_CLIENT", created.factory)
    yield created
    get_settings.cache_clear()


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    # stdout also carries the structured `marketplace.call` log lines: the result is the last line
    lines = [line for line in out.out.splitlines() if "marketplace.call" not in line]
    clean(out.out, out.err)
    return code, "".join(f"{line}\n" for line in lines), out.err


def refresh(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    return run(capsys, "templates", "refresh", "--product-type", "KURTA", *STAFF, *extra)


def rows(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT product_type_version, is_current FROM product_type_definitions ORDER BY created_at"
    ).fetchall()


def audits(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT action, actor_email FROM audit_log WHERE entity_type = 'product_type_definition'"
    ).fetchall()


SECRETS = ("X-Amz-", "ptd-schemas.fake", "__ptd_schema", '"$defs"', PLATFORM_TOKEN, "Zq9")


def clean(*texts: str) -> None:
    for text in texts:
        for leak in SECRETS:
            assert leak not in text, leak


def test_refresh_store_rerun_change_promote(
    fake: Fake, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = refresh(capsys)
    assert (code, err) == (0, "")
    assert out.startswith("templates refresh: stored_current KURTA version UFAKE0001 digest ")
    assert rows(superuser_conn) == [("UFAKE0001", True)]
    assert sorted(audits(superuser_conn)) == [
        ("product_type_definition.promoted", "staff@example.com"),
        ("product_type_definition.stored", "staff@example.com"),
    ]
    code, out, err = refresh(capsys)
    assert code == 0 and "unchanged" in out
    assert len(audits(superuser_conn)) == 2
    fake.control(schema="KURTA.drift.schema.json")
    code, out, err = refresh(capsys)
    assert code == 0 and "templates refresh: stored KURTA" in out
    assert rows(superuser_conn) == [("UFAKE0001", True), ("UFAKE0001", False)]
    digest = out.split(" digest ")[1].split()[0]
    code, out, err = run(
        capsys,
        "templates",
        "promote-definition",
        "--product-type",
        "KURTA",
        "--version",
        "UFAKE0001",
        "--digest",
        digest,
        *STAFF,
    )
    assert (code, err) == (0, "") and "promoted KURTA" in out
    assert rows(superuser_conn) == [("UFAKE0001", False), ("UFAKE0001", True)]
    code, out, err = run(
        capsys,
        "templates",
        "promote-definition",
        "--product-type",
        "KURTA",
        "--version",
        "U9",
        *STAFF,
    )
    assert code == 4 and "not found" in err
    clean(out, err)


def test_dry_run_needs_no_database(
    fake: Fake,
    superuser_conn: Conn,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.config import get_settings

    monkeypatch.delenv("ALEMBIC_DATABASE_URL")
    get_settings.cache_clear()
    code, out, err = refresh(capsys, "--dry-run")
    assert (code, err) == (0, "") and "fetched KURTA version UFAKE0001" in out
    assert rows(superuser_conn) == []
    saved = tmp_path / "KURTA.json"
    code, out, err = refresh(capsys, "--dry-run", "--save-schema", str(saved))
    assert code == 0
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert json.loads(saved.read_text(encoding="utf-8")) == json.loads(
        ptd_fixture("KURTA.listing.schema.json")  # SI2: LISTING is the default
    )
    code, out, err = refresh(capsys, "--dry-run", "--save-schema", str(saved))
    assert code == 1 and "not saved" in err
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    link.symlink_to(target)
    code, out, err = refresh(capsys, "--dry-run", "--save-schema", str(link))
    assert code == 1 and not target.exists()
    clean(out, err)


def test_missing_platform_token(
    fake: Fake, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.delenv("AMAZON_PLATFORM_REFRESH_TOKEN")
    get_settings.cache_clear()
    code, out, err = refresh(capsys)
    assert (code, out) == (1, "")
    assert err == "templates refresh: failed (AMAZON_PLATFORM_REFRESH_TOKEN is required)\n"


def test_statements_touch_no_tenant_table(fake: Fake, capsys: pytest.CaptureFixture[str]) -> None:
    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement.lower())

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        code, _, _ = refresh(capsys)
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert code == 0 and statements
    joined = "\n".join(statements)
    assert "marketplace_accounts" not in joined
    for table in ("listings", "products", "tenants", "job_runs"):
        assert f" {table} " not in joined and f"public.{table}" not in joined
    assert "product_type_definitions" in joined and "audit(" in joined


def test_fake_faults_write_nothing(
    fake: Fake, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.control(bad_checksum=True)
    code, out, err = refresh(capsys)
    assert code == 1 and err == "templates refresh: failed (SchemaInvalid, SCHEMA_INVALID)\n"
    fake.control(status=[429, 429, 429, 429])
    code, out, err = refresh(capsys)
    assert code == 1 and "RateLimited" in err
    assert rows(superuser_conn) == []
    clean(out, err)


def test_diff_stored_and_missing(
    fake: Fake, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    diff = ("templates", "diff", "--product-type", "KURTA")
    code, out, err = run(capsys, *diff)
    assert code == 3 and "no stored definition" in err
    fake.control(schema="KURTA.drift.schema.json")
    assert refresh(capsys)[0] == 0
    code, out, err = run(capsys, *diff)
    assert code == 1 and "neck_style" in out and "item_type_keyword" in out
    clean(out, err)
    fake.control(schema="KURTA.schema.json")
    refresh(capsys)
    _, out, _ = refresh(capsys)
    # the matching fixture went in non-current: promote it, then no drift
    digest = out.split(" digest ")[1].split()[0]
    run(
        capsys,
        "templates",
        "promote-definition",
        "--product-type",
        "KURTA",
        "--version",
        "UFAKE0001",
        "--digest",
        digest,
        *STAFF,
    )
    code, out, err = run(capsys, *diff)
    assert (code, err) == (0, "") and "0 drift(s)" in out
