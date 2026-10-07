"""S13 B7 (PD11, AC 19, AC 25; DATABASE.md:523-545): `python -m app.cli templates load`, the
versioned, migrator-only template loader, and `promote_template`."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.cli import main
from app.db.platform_loads import load_template_file, promote_template
from psycopg.types.json import Jsonb
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import LOCAL_HOSTS, Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/imports/templates_missing_category.json"
CATEGORY = "ethnic_wear_dupattas"
EMPTY_TEMPLATE = Jsonb({"required_attributes": [], "optional_attributes": [], "specs": {}})


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


def entry(version: int, **template: Any) -> dict[str, Any]:
    return {
        "version": version,
        "product_type": "DUPATTA",
        "template": {"marketplace": "amazon", "category": CATEGORY, **template},
    }


def write(tmp: Path, entries: list[dict[str, Any]], name: str = "t.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def rows(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT version, is_current, source, raw_schema_key, product_type FROM category_templates "
        "WHERE category = %s ORDER BY version",
        [CATEGORY],
    ).fetchall()


def audits(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT tenant_id, actor_type, action, entity_type, after FROM audit_log "
        "WHERE entity_type = 'category_template' ORDER BY occurred_at, id"
    ).fetchall()


def run(path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["templates", "load", "--file", str(path)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_templates_load_idempotent(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    before = len(audits(superuser_conn))
    assert run(FIXTURE, capsys) == (
        0, "templates load: ok (1 inserted, 0 unchanged, 1 promoted)\n", ""
    )  # fmt: skip
    assert rows(superuser_conn) == [(1, True, "manual", None, "DUPATTA")]
    after_first = len(audits(superuser_conn))
    assert after_first == before + 1
    assert run(FIXTURE, capsys) == (
        0, "templates load: ok (0 inserted, 1 unchanged, 0 promoted)\n", ""
    )  # fmt: skip
    assert rows(superuser_conn) == [(1, True, "manual", None, "DUPATTA")]
    assert len(audits(superuser_conn)) == after_first


def test_templates_load_content_change_exit_1_no_rows(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(FIXTURE, capsys)[0] == 0
    changed = write(
        cli_env,
        [entry(1, required_attributes=["color"]), entry(2), {**entry(1), "product_type": "X"}],
    )
    code, out, err = run(changed, capsys)
    assert code == 1 and out == ""
    assert err == (
        f"templates load: refused: amazon/{CATEGORY} v1 already holds other content; "
        "bump version (nothing was written)\n"
    )
    assert rows(superuser_conn) == [(1, True, "manual", None, "DUPATTA")]


def test_templates_load_new_version_promotes(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(FIXTURE, capsys)[0] == 0
    newer = write(cli_env, [entry(2, required_attributes=["fabric_type"])])
    assert run(newer, capsys)[1] == "templates load: ok (1 inserted, 0 unchanged, 1 promoted)\n"
    assert [(v, c) for v, c, *_ in rows(superuser_conn)] == [(1, False), (2, True)]
    older = write(cli_env, [entry(3, required_attributes=["pattern"]), entry(2, required_attributes=["fabric_type"])], "o.json")  # fmt: skip
    assert run(older, capsys)[0] == 0
    current = superuser_conn.execute(
        "SELECT count(*), max(version) FROM category_templates "
        "WHERE category = %s AND is_current",
        [CATEGORY],
    ).fetchone()
    assert current == (1, 3)


def test_templates_load_never_raises_grd01(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every leg (insert, no-op, conflict, newer version, already current) ends on our own exit
    line: the immutable-content guard (GRD01) never fires."""
    legs = [
        FIXTURE,
        FIXTURE,
        write(cli_env, [entry(1, required_attributes=["color"])], "conflict.json"),
        write(cli_env, [entry(2)], "v2.json"),
        write(cli_env, [entry(2)], "v2.json"),
    ]
    for path in legs:
        code, _out, err = run(path, capsys)
        assert "GRD01" not in err and "SQLSTATE" not in err, err
        assert code in (0, 1)
    assert [(v, c) for v, c, *_ in rows(superuser_conn)] == [(1, False), (2, True)]


async def test_promote_template_two_updates_one_tx(cli_env: Path, superuser_conn: Conn) -> None:
    for version in (1, 2):
        superuser_conn.execute(
            "INSERT INTO category_templates (marketplace, category, version, is_current, "
            "product_type, source, attributes) VALUES ('amazon', %s, %s, %s, 'D', 'manual', %s)",
            [CATEGORY, version, version == 1, EMPTY_TEMPLATE],
        )
    engine = create_async_engine(os.environ["DATABASE_URL"])
    seen: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith(("UPDATE", "BEGIN", "COMMIT", "ROLLBACK")):
            seen.append(statement.split()[0].upper())

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            assert await promote_template(connection, "amazon", CATEGORY, 2)
            assert seen == ["UPDATE", "UPDATE"]
            await transaction.rollback()
        assert [(v, c) for v, c, *_ in rows(superuser_conn)] == [(1, True), (2, False)]
        async with engine.begin() as connection:
            assert await promote_template(connection, "amazon", CATEGORY, 2)
            assert not await promote_template(connection, "amazon", CATEGORY, 2)  # already
        assert [(v, c) for v, c, *_ in rows(superuser_conn)] == [(1, False), (2, True)]
        async with engine.begin() as connection:
            with pytest.raises(LookupError):
                await promote_template(connection, "amazon", CATEGORY, 9)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("case", ["version_0", "missing_version", "bad_template", "not_json"])
def test_templates_load_invalid_file_exit_1_no_rows(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    good = entry(5)
    bodies: dict[str, Any] = {
        "version_0": [good, entry(0)],
        "missing_version": [good, {k: v for k, v in entry(1).items() if k != "version"}],
        "bad_template": [good, entry(6, category="Not A Slug")],
    }
    path = cli_env / "bad.json"
    if case == "not_json":
        path.write_text("[{", encoding="utf-8")
    else:
        path.write_text(json.dumps(bodies[case]), encoding="utf-8")
    code, out, err = run(path, capsys)
    assert code == 1 and out == ""
    assert err.startswith("templates load: invalid file (") and "nothing was written" in err
    assert rows(superuser_conn) == []
    with pytest.raises(ValueError):
        load_template_file(path)


def test_templates_load_missing_file_exit_2(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(cli_env / "absent.json", capsys) == (2, "", "templates load: no such file\n")


def test_templates_load_audited_null_tenant(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(FIXTURE, capsys)[0] == 0
    tenant, actor, action, entity, after = audits(superuser_conn)[-1]
    assert (tenant, actor, action, entity) == (
        None, "system", "category_template.promoted", "category_template",
    )  # fmt: skip
    assert after == {"marketplace": "amazon", "category": CATEGORY}
