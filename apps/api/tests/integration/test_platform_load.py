"""S20 T5b (AC19; D12; C18): `python -m app.cli platform load`, the one platform-load path the
tenant-less acceptance stack and `seed` share."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.cli import main

from .conftest import LOCAL_HOSTS, Conn

pytestmark = [pytest.mark.allow_hosts(LOCAL_HOSTS), pytest.mark.usefixtures("clean_tenant_db")]


@pytest.fixture
def cli_env(db_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def run(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    code = main(["platform", "load", *extra])
    out = capsys.readouterr()
    return code, out.out, out.err


def counts(conn: Conn) -> dict[str, int]:
    out = {}
    for table in ("tenants", "category_templates", "rules", "marketplace_error_map", "audit_log"):
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 - fixed names
        assert row is not None
        out[table] = int(str(row[0]))
    return out


def test_platform_load_idempotent_no_tenants(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert out == (
        "platform load: ok (2 templates inserted, 2 promoted, 4 error-map rows inserted, "
        "0 toggled)\n"
    )
    first = counts(superuser_conn)
    assert first["tenants"] == 0 and first["marketplace_error_map"] == 4
    code, out, _ = run(capsys)
    assert code == 0 and "0 templates inserted, 0 promoted, 0 error-map rows inserted" in out
    assert counts(superuser_conn) == first  # no row and no audit from the rerun
    tshirt_rules = superuser_conn.execute(
        "SELECT marketplace, count(*) FROM rules WHERE category = 'tshirts' AND tenant_id IS NULL "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in tshirt_rules] == ["amazon", "flipkart"]
    current = superuser_conn.execute(
        "SELECT category, version, product_type FROM category_templates "
        "WHERE marketplace = 'amazon' AND is_current ORDER BY category"
    ).fetchall()
    assert ("tshirts", 1, "SHIRT") in current and ("ethnic_wear_kurta_sets", 2, "KURTA") in current


@pytest.mark.parametrize("env", ["staging", "production"])
def test_platform_load_refused_outside_local(
    cli_env: Path,
    superuser_conn: Conn,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    env: str,
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("APP_ROLE", "seed")
    monkeypatch.setenv("JWT_SECRET", "s20-staging-like-secret-0123456789abcdef")
    get_settings.cache_clear()
    code, out, err = run(capsys)
    assert (code, out) == (3, "")
    assert err.startswith(f"platform load: refused: APP_ENV={env}")
    assert counts(superuser_conn)["category_templates"] == 0


def test_platform_load_bad_error_map_exit_1_nothing_written(
    cli_env: Path, superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/error_map.fake.json").read_text("utf-8")
    )
    rows[1]["plain_text"] = "{0.__class__}"
    bad = cli_env / "bad.json"
    bad.write_text(json.dumps(rows), encoding="utf-8")
    code, _, err = run(capsys, "--error-map", str(bad))
    assert code == 1 and err == "platform load: invalid file (ValidationError)\n"
    assert counts(superuser_conn)["marketplace_error_map"] == 0
    assert counts(superuser_conn)["category_templates"] == 0  # the file is read before any write


def test_platform_load_missing_error_map_exit_2(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "--error-map", str(cli_env / "absent.json")) == (
        2,
        "",
        "platform load: no such file\n",
    )


async def test_platform_load_then_seed_ok(cli_env: Path, superuser_conn: Conn) -> None:
    """The acceptance stack's order: platform rows first, then the demo seed. Kurta v2 is
    already current when KURTA-001 is seeded, so it pins v2 on this path."""
    from app.cli import ERROR_MAP_FAKE_PATH, run_platform_load, run_seed
    from app.db.seed_ops import demo_listing_ids

    assert await run_platform_load(ERROR_MAP_FAKE_PATH) == 0
    assert await run_seed("demo", reset=False) == 0

    run_id = demo_listing_ids()[2]
    pinned = superuser_conn.execute(
        "SELECT template_version FROM validation_runs WHERE id = %s", [run_id]
    ).fetchone()
    assert pinned == (2,)
