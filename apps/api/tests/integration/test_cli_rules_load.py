"""`python -m app.cli rules load [--file PATH]`: the migrator-only loader of the platform RuleSets
(`rules.seed.json` by default) that the deploy's migrate job runs after `dictionary sync`.

A rule is matched on its fixed `id`, and a new id on `rules_scope_key` (0002_listing_pipeline
:116-119): the same content is a no-op, a difference in `enabled` alone flips it (the resolver's
rollback lever, rules_engine/resolver.py), any other difference or a new id on a stored scope key
refuses the whole file. Each write is audited with a NULL tenant and only `{enabled}`."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app.cli import RULES_SEED_PATH, main

from .conftest import LOCAL_HOSTS, Conn, libpq_url

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
SEED: list[dict[str, Any]] = json.loads(RULES_SEED_PATH.read_text(encoding="utf-8"))
SEED_RULES = sum(len(rule_set["rules"]) for rule_set in SEED)
RULE_ID = "5eed0000-0000-4000-8000-0000ffff0001"
OTHER_ID = "5eed0000-0000-4000-8000-0000ffff0002"


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


def rule(**over: Any) -> dict[str, Any]:
    """The seed's first rule under a test id; `rule_def` keys go in `rule_def=`."""
    row: dict[str, Any] = copy.deepcopy(SEED[0]["rules"][0])
    row["id"] = RULE_ID
    row.update(over)
    return row


def rule_set(*rules: dict[str, Any]) -> list[dict[str, Any]]:
    head = {k: v for k, v in SEED[0].items() if k != "rules"}
    return [{**head, "rules": list(rules)}]


def write(tmp: Path, body: Any, name: str = "r.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def rows(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id::text, enabled, revision, rule_def ->> 'message' FROM rules ORDER BY id"
    ).fetchall()


def audits(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT tenant_id, actor_type, action, before, after FROM audit_log "
        "WHERE entity_type = 'rules' ORDER BY occurred_at, id"
    ).fetchall()


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["rules", "load", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_rules_load_default_file_inserts_then_reruns_as_noop(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys) == (
        0, f"rules load: ok ({SEED_RULES} inserted, 0 unchanged, 0 toggled)\n", ""
    )  # fmt: skip
    first = rows(superuser_conn)
    assert len(first) == SEED_RULES
    tenants = superuser_conn.execute("SELECT count(*) FROM rules WHERE tenant_id IS NOT NULL")
    assert tenants.fetchone() == (0,)
    after_first = len(audits(superuser_conn))
    assert after_first == SEED_RULES
    assert run(capsys) == (
        0, f"rules load: ok (0 inserted, {SEED_RULES} unchanged, 0 toggled)\n", ""
    )  # fmt: skip
    assert rows(superuser_conn) == first  # no write: every revision stays where it was
    assert len(audits(superuser_conn)) == after_first


def test_rules_load_matches_the_seed_path(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    """`seed` (on-conflict-do-nothing) and `rules load` write the same rows: a load after the
    seed's platform rows finds every rule unchanged."""
    import asyncio

    from app.db.seed_ops import seed_platform_rows
    from sqlalchemy.ext.asyncio import create_async_engine

    async def seed() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                await seed_platform_rows(connection, RULES_SEED_PATH)
        finally:
            await engine.dispose()

    asyncio.run(seed())
    assert run(capsys) == (
        0, f"rules load: ok (0 inserted, {SEED_RULES} unchanged, 0 toggled)\n", ""
    )  # fmt: skip


@pytest.mark.parametrize("start", [True, False], ids=["turn off", "turn back on"])
def test_rules_load_enabled_flip(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str], start: bool
) -> None:
    assert run(capsys, "--file", str(write(cli_env, rule_set(rule(enabled=start)))))[0] == 0
    ((_, enabled, revision, _),) = rows(superuser_conn)
    assert enabled is start
    flip = write(cli_env, rule_set(rule(enabled=not start)), "flip.json")
    assert run(capsys, "--file", str(flip)) == (
        0, "rules load: ok (0 inserted, 0 unchanged, 1 toggled)\n", ""
    )  # fmt: skip
    ((_, enabled, flipped, _),) = rows(superuser_conn)
    assert enabled is (not start) and flipped > revision  # rules_guard takes a new revision
    assert audits(superuser_conn)[-1] == (
        None, "system", "rules.enabled_changed", {"enabled": start}, {"enabled": not start}
    )  # fmt: skip
    assert run(capsys, "--file", str(flip))[1] == (
        "rules load: ok (0 inserted, 1 unchanged, 0 toggled)\n"
    )


def drop_rule_def_keys(conn: Conn, rule_id: str, *keys: str) -> None:
    """Rewrite a stored rule_def as an older schema wrote it (`rules_guard` bypassed)."""
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        for key in keys:
            path = "{" + key.replace(".", ",") + "}"
            conn.execute(
                "UPDATE rules SET rule_def = rule_def #- %s::text[] WHERE id = %s",
                [path, rule_id],
            )


def test_rules_load_row_from_an_older_schema_is_unchanged(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row loaded before the schema gained an optional field lacks that key; the current
    model fills it with its default, so the row is the same content, not a refusal."""
    path = write(cli_env, rule_set(rule()))
    assert run(capsys, "--file", str(path))[0] == 0
    stored = rule()["rule_def"]
    assert stored["suggestion"] is None and stored["policy_ref"]["url"] is None
    drop_rule_def_keys(superuser_conn, RULE_ID, "suggestion", "retry_class", "policy_ref.url")
    before = rows(superuser_conn), len(audits(superuser_conn))
    assert run(capsys, "--file", str(path)) == (
        0, "rules load: ok (0 inserted, 1 unchanged, 0 toggled)\n", ""
    )  # fmt: skip
    assert (rows(superuser_conn), len(audits(superuser_conn))) == before


def test_rules_load_id_of_a_stored_tenant_rule_refused(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    banned = copy.deepcopy(SEED[0]["rules"][2])
    assert banned["rule_def"]["type"] == "banned_terms" and banned["rule_def"]["match"] != "regex"
    platform = {**banned, "id": RULE_ID}
    path = write(cli_env, rule_set(platform))
    assert run(capsys, "--file", str(path))[0] == 0
    with superuser_conn.transaction():
        superuser_conn.execute("SET LOCAL session_replication_role = replica")
        superuser_conn.execute(
            "UPDATE rules SET tenant_id = '5eed0000-0000-4000-8000-00000000aaaa' WHERE id = %s",
            [RULE_ID],
        )
    before = rows(superuser_conn)
    code, out, err = run(capsys, "--file", str(path))
    assert (code, out) == (1, "")
    assert err == (
        f"rules load: refused: rule {RULE_ID} is the id of a stored tenant rule "
        "(nothing was written)\n"
    )
    assert rows(superuser_conn) == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"severity": "warning"}, id="severity"),
        pytest.param({"field": "content.description"}, id="field"),
        pytest.param({"version": 2}, id="version"),
        pytest.param({"category": "tshirts"}, id="category"),
        pytest.param({"message": "other text"}, id="rule_def message"),
        pytest.param({"limit": 150}, id="rule_def limit"),
        pytest.param({"pack": "seo"}, id="pack"),
        pytest.param({"_base": 4, "attribute": "fabric_type"}, id="subject"),
    ],
)
def test_rules_load_content_change_refused_nothing_written(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str], change: dict[str, Any]
) -> None:
    change = dict(change)
    base = {**copy.deepcopy(SEED[0]["rules"][change.pop("_base", 0)]), "id": RULE_ID}
    assert run(capsys, "--file", str(write(cli_env, rule_set(base))))[0] == 0
    before = rows(superuser_conn), len(audits(superuser_conn))
    changed = copy.deepcopy(base)
    for key, value in change.items():
        if key in changed:
            changed[key] = value
        else:
            changed["rule_def"][key] = value
    other = rule(id=OTHER_ID, field="content.bullets")  # a new rule in the same file
    path = write(cli_env, rule_set(other, changed), "changed.json")
    code, out, err = run(capsys, "--file", str(path))
    assert (code, out) == (1, "")
    assert err == (
        f"rules load: refused: rule {RULE_ID} already holds other content; bump version "
        "(nothing was written)\n"
    )
    assert (rows(superuser_conn), len(audits(superuser_conn))) == before  # `other` rolled back


def test_rules_load_new_id_on_a_stored_scope_key_refused(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "--file", str(write(cli_env, rule_set(rule()))))[0] == 0
    before = rows(superuser_conn)
    twin = write(cli_env, rule_set(rule(id=OTHER_ID)), "twin.json")
    code, out, err = run(capsys, "--file", str(twin))
    assert (code, out) == (1, "")
    assert err == (
        f"rules load: refused: rule {OTHER_ID} collides with a stored rule on rules_scope_key "
        "(nothing was written)\n"
    )
    assert rows(superuser_conn) == before


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(rule(enabled=False), id="same id twice"),
        pytest.param(rule(id=OTHER_ID), id="same scope key twice"),
    ],
)
def test_rules_load_duplicate_in_file_refused(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str], second: dict[str, Any]
) -> None:
    code, out, err = run(capsys, "--file", str(write(cli_env, rule_set(rule(), second))))
    assert (code, out) == (1, "")
    assert err.startswith("rules load: refused: rule ") and "(nothing was written)" in err
    assert rows(superuser_conn) == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(rule_set(rule(tenant_id="5eed0000-0000-4000-8000-00000000aaaa")), id="tenant"),
        pytest.param(rule_set(rule(severity="fatal")), id="bad severity"),
        pytest.param([{"marketplace": "amazon"}], id="not a rule set"),
        pytest.param({"rules": []}, id="not a list"),
    ],
)
def test_rules_load_invalid_file_exit_1_nothing_written(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str], body: Any
) -> None:
    code, out, err = run(capsys, "--file", str(write(cli_env, body)))
    assert (code, out) == (1, "")
    assert err.startswith("rules load: invalid file (") and "nothing was written" in err
    assert rows(superuser_conn) == []


def test_rules_load_not_json_exit_1(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cli_env / "bad.json"
    path.write_text("[{", encoding="utf-8")
    code, _, err = run(capsys, "--file", str(path))
    assert code == 1 and err.startswith("rules load: invalid file (")
    assert rows(superuser_conn) == []


def test_rules_load_missing_file_exit_2(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    absent = cli_env / "absent.json"
    assert run(capsys, "--file", str(absent)) == (2, "", "rules load: no such file\n")


def test_rules_load_waits_on_the_migration_lock_then_fails_closed(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load holds the migration advisory lock for its transaction, with the migrations'
    lock_timeout: an overlapping migrate job fails fast and writes nothing."""
    from app.db.migrations import MIGRATION_LOCK_KEY

    superuser_conn.execute("SELECT pg_advisory_lock(%s)", [MIGRATION_LOCK_KEY])
    try:
        code, out, err = run(capsys, "--file", str(write(cli_env, rule_set(rule()))))
    finally:
        superuser_conn.execute("SELECT pg_advisory_unlock(%s)", [MIGRATION_LOCK_KEY])
    assert (code, out) == (1, "")
    assert err.startswith("rules load: failed (") and "55P03" in err
    assert rows(superuser_conn) == []


def test_rules_load_audited_null_tenant_enabled_only(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.repositories.audit import AUDITED_ENTITIES

    assert "rules" in AUDITED_ENTITIES
    assert run(capsys, "--file", str(write(cli_env, rule_set(rule()))))[0] == 0
    (audit,) = audits(superuser_conn)
    assert audit == (None, "system", "rules.loaded", None, {"enabled": True})
    entity = superuser_conn.execute(
        "SELECT entity_id::text, reason FROM audit_log WHERE entity_type = 'rules'"
    ).fetchone()
    assert entity == (RULE_ID, "rules load")


def test_rules_load_without_migrator_url_exit_1(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.core.config import get_settings

    monkeypatch.delenv("ALEMBIC_DATABASE_URL")
    get_settings.cache_clear()
    assert run(capsys) == (1, "", "rules load: failed (ALEMBIC_DATABASE_URL is required)\n")


def test_app_rw_cannot_write_a_null_tenant_rule(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rules table holds brand rows too (`rules_tenant_types`), so app_rw keeps a column
    INSERT and `UPDATE (enabled)`; RLS confines both to its own tenant. A platform row is out of
    its reach: no INSERT with a NULL tenant, a toggle matches no row, and no DELETE at all."""
    from tests.support.dsn import as_app_rw

    assert run(capsys, "--file", str(write(cli_env, rule_set(rule()))))[0] == 0
    before = rows(superuser_conn)
    url = libpq_url(as_app_rw(os.environ["DATABASE_URL"]))
    with psycopg.connect(url, autocommit=True) as app_rw:
        assert app_rw.execute("SELECT count(*) FROM rules").fetchone() == (1,)  # it reads them
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_rw.execute(
                "INSERT INTO rules (tenant_id, marketplace, field, rule_type, subject, rule_def) "
                "VALUES (NULL, 'amazon', 'content.title', 'banned_terms', '', "
                '\'{"type": "banned_terms", "policy_ref": {}}\')'
            )
        toggled = app_rw.execute("UPDATE rules SET enabled = false WHERE tenant_id IS NULL")
        assert toggled.rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_rw.execute("DELETE FROM rules")
    assert rows(superuser_conn) == before
