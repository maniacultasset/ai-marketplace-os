"""S9 T12, T13, T21 (AC 5, 6, 13; A§5, PLAN D9, D13, D16): `python -m app.cli seed`.

Every test seeds inside itself (`clean_tenant_db` empties mos_test first) and never relies on
another test's seed. Sockets stay disabled except to the local database, and no
`OPENAI_API_KEY` is set: the seed makes no network or model call."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from . import ops_support as ops
from .conftest import LOCAL_HOSTS, REPO_ROOT

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("seed_env", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]
NON_COMMITTED_SECRET = "s9-staging-like-secret-0123456789abcdef"
RULES_SEED = REPO_ROOT / "packages/schemas/fixtures/rules.seed.json"


@pytest.fixture
def seed_env(db_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    from app.core.config import get_settings

    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def _seed(profile: str, *, reset: bool = False) -> int:
    from app.cli import run_seed

    return await run_seed(profile, reset=reset)


def _ids() -> dict[str, UUID]:
    from app.db.seed_ops import seed_tenant_id

    return {slug: seed_tenant_id(slug) for slug in ("acme", "bharat")}


def _scalar(conn: Conn, query: str, params: list[Any] | None = None) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _per_tenant(conn: Conn, table: str) -> dict[UUID, int]:
    query = sql.SQL("SELECT tenant_id, count(*) FROM {} GROUP BY 1").format(sql.Identifier(table))
    return {r[0]: int(r[1]) for r in conn.execute(query).fetchall()}


def _all_counts(conn: Conn) -> dict[str, int]:
    from app.db.base import metadata

    counts = {}
    for table in metadata.sorted_tables:
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table.name))
        counts[table.name] = int(_scalar(conn, query.as_string(conn)))
    return counts


# ---- T12: tenants, users, memberships, products ----


async def test_seed_profiles_demo_counts(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    ids = _ids()
    assert _scalar(superuser_conn, "SELECT count(*) FROM tenants") == 2
    assert _per_tenant(superuser_conn, "products") == {ids["acme"]: 4, ids["bharat"]: 4}


async def test_seed_profiles_isolation_counts(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    ids = _ids()
    assert _per_tenant(superuser_conn, "products") == {ids["acme"]: 3, ids["bharat"]: 3}


async def test_seed_isolation_after_demo_counts(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    assert await _seed("isolation", reset=True) == 0
    ids = _ids()
    assert _per_tenant(superuser_conn, "products") == {ids["acme"]: 3, ids["bharat"]: 3}
    assert _per_tenant(superuser_conn, "brand_chunks") == {}


async def test_seed_profiles_one_membership_per_role(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    rows = superuser_conn.execute(
        "SELECT t.slug, m.role, u.email FROM tenant_memberships m "
        "JOIN tenants t ON t.id = m.tenant_id JOIN users u ON u.id = m.user_id ORDER BY 1, 2"
    ).fetchall()
    expected = sorted(
        (slug, role, f"{role}@{slug}.test")
        for slug in ("acme", "bharat")
        for role in ("owner", "operator", "reviewer", "admin")
    )
    assert sorted(rows) == expected


async def test_seed_shared_skus_in_both_tenants(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    rows = superuser_conn.execute(
        "SELECT sku FROM products GROUP BY sku HAVING count(DISTINCT tenant_id) = 2 ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == ["DECOR-001", "KURTA-001", "KURTA-002"]
    singles = superuser_conn.execute(
        "SELECT t.slug, p.sku FROM products p JOIN tenants t ON t.id = p.tenant_id "
        "WHERE p.sku LIKE '%-900' ORDER BY 1"
    ).fetchall()
    assert singles == [("acme", "ACME-900"), ("bharat", "BH-900")]


async def test_seed_password_verifies(superuser_conn: Conn) -> None:
    from app.core.passwords import verify_password

    assert await _seed("isolation") == 0
    hashed = _scalar(
        superuser_conn, "SELECT hashed_password FROM users WHERE email = 'owner@acme.test'"
    )
    assert hashed.startswith("$argon2id$")
    assert verify_password("devpassword123", hashed)
    assert not verify_password("wrong", hashed)


async def test_seed_idempotent(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    first = _all_counts(superuser_conn)
    hashed = _scalar(superuser_conn, "SELECT hashed_password FROM users ORDER BY email LIMIT 1")
    assert await _seed("demo") == 0
    assert _all_counts(superuser_conn) == first
    again = _scalar(superuser_conn, "SELECT hashed_password FROM users ORDER BY email LIMIT 1")
    assert again == hashed  # hashed only on insert


async def test_seed_output_has_no_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    assert await _seed("demo") == 0
    out = capsys.readouterr()
    assert out.out == "seed: ok (profile demo, 2 tenants, 8 products, 8 users)\n"
    for leak in ("devpassword123", "postgres:", "@localhost", "argon2"):
        assert leak not in out.out + out.err


@pytest.mark.parametrize("app_env", ["staging", "production"])
async def test_seed_refused_outside_local(
    superuser_conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    app_env: str,
) -> None:
    before = _all_counts(superuser_conn)
    _env(monkeypatch, APP_ENV=app_env, APP_ROLE="seed", JWT_SECRET=NON_COMMITTED_SECRET)
    assert await _seed("demo") == 3
    out = capsys.readouterr()
    assert out.err == f"seed: refused: APP_ENV={app_env} (seed runs only under APP_ENV=local)\n"
    assert out.out == ""
    assert _all_counts(superuser_conn) == before
    assert _scalar(superuser_conn, "SELECT count(*) FROM users WHERE email LIKE '%@acme.test'") == 0


async def test_seed_tenant_id_mismatch_exit_1(
    superuser_conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    squatter = f.tenant(superuser_conn, slug="acme", name="Not the seed")
    assert await _seed("demo") == 1
    assert capsys.readouterr().err == "seed: failed (SeedTenantIdMismatch)\n"
    assert _scalar(superuser_conn, "SELECT id FROM tenants WHERE slug = 'acme'") == squatter
    assert _per_tenant(superuser_conn, "products") == {}
    assert _scalar(superuser_conn, "SELECT count(*) FROM users WHERE email LIKE '%@acme.test'") == 0


async def test_seed_failure_line_class_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A driver failure prints the class and SQLSTATE only, never the DSN or the message."""
    bad = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/mos_s9_no_such_db"
    _env(monkeypatch, ALEMBIC_DATABASE_URL=bad)
    assert await _seed("demo") == 1
    err = capsys.readouterr().err
    assert err.startswith("seed: failed (OperationalError")
    for leak in ("mos_s9_no_such_db", "postgres", "localhost", "@"):
        assert leak not in err


# ---- T13: platform rows, brand data, --reset ----


async def test_seed_category_templates_present(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    rows = superuser_conn.execute(
        "SELECT marketplace, category FROM category_templates WHERE is_current ORDER BY 1, 2"
    ).fetchall()
    assert rows == [
        ("amazon", "ethnic_wear_kurta_sets"),
        ("amazon", "home_decor_lamps"),
        ("flipkart", "ethnic_wear_kurta_sets"),
        ("flipkart", "home_decor_lamps"),
    ]


def _fixture_rule_ids() -> set[UUID]:
    data = json.loads(RULES_SEED.read_text(encoding="utf-8"))
    return {UUID(rule["id"]) for rule_set in data for rule in rule_set["rules"]}


async def test_seed_platform_rules_null_tenant(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    rows = superuser_conn.execute("SELECT id, tenant_id FROM rules").fetchall()
    assert {r[0] for r in rows} == _fixture_rule_ids()
    assert {r[1] for r in rows} == {None}


async def test_seed_demo_guidelines_and_brand_profile(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    rows = superuser_conn.execute(
        "SELECT t.slug, jsonb_array_length(t.guidelines), t.glossary IS NOT NULL, "
        "b.tenant_id IS NOT NULL FROM tenants t "
        "LEFT JOIN brand_profiles b ON b.tenant_id = t.id ORDER BY 1"
    ).fetchall()
    assert rows == [("acme", 2, True, True), ("bharat", 2, True, True)]


async def test_seed_demo_brand_chunks_searchable() -> None:
    from app.core.vectors import hash_vector
    from app.db.session import unit_of_work
    from app.repositories.brand_chunks import BrandChunkRepository

    assert await _seed("demo") == 0
    acme = _ids()["acme"]
    async with unit_of_work(acme) as session:
        found = await BrandChunkRepository(session, acme).search(hash_vector("acme:glossary:0"))
    assert len(found) == 4
    assert found[0].type == "glossary"
    assert {c.tenant_id for c in found} == {acme}


async def test_seed_isolation_has_no_brand_chunks(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    assert _scalar(superuser_conn, "SELECT count(*) FROM brand_chunks") == 0
    assert _scalar(superuser_conn, "SELECT count(*) FROM tenants WHERE guidelines IS NULL") == 2


@pytest.mark.parametrize("app_env", ["staging", "production"])
async def test_seed_reset_refused_outside_local(
    superuser_conn: Conn, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    assert await _seed("demo") == 0
    before = _all_counts(superuser_conn)
    _env(monkeypatch, APP_ENV=app_env, APP_ROLE="seed", JWT_SECRET=NON_COMMITTED_SECRET)
    assert await _seed("isolation", reset=True) == 3
    assert _all_counts(superuser_conn) == before


async def test_seed_reset_clears_later_tables(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    acme = _ids()["acme"]
    other = ops.seed_tenant(superuser_conn)
    for side in (acme, other.tenant_id):
        listing_id = f.listing(superuser_conn, side)
        version_id = f.version(superuser_conn, side, listing_id)
        ops.job_run(superuser_conn, side)
        f.audit(superuser_conn, side)
        f.agent_run(superuser_conn, side)
        f.validation_run(superuser_conn, side, listing_id)
        f.review_decision(superuser_conn, side, listing_id, version_id)
    tables = (
        "job_runs",
        "listings",
        "audit_log",
        "agent_runs",
        "validation_runs",
        "review_decisions",
    )
    # a successful-login audit row for acme, written as the app writes it (audit() as app_rw):
    # 0004 makes audit_log append-only, so the reset must go through purge_tenant
    owner = superuser_conn.execute(
        "SELECT id FROM users WHERE lower(email) = 'owner@acme.test'"
    ).fetchone()
    assert owner is not None
    with superuser_conn.transaction():  # the login's unit of work: acme's GUC (0005), app_rw
        superuser_conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(acme)])
        superuser_conn.execute("SET LOCAL ROLE app_rw")
        superuser_conn.execute(
            "SELECT audit(%s, 'user', %s, 'auth.login', 'user', %s, NULL, NULL, NULL, "
            "'req-login', NULL, '10.0.0.9', NULL)",
            (acme, owner[0], owner[0]),
        )
    others = {t: _per_tenant(superuser_conn, t).get(other.tenant_id) for t in tables}
    assert await _seed("isolation", reset=True) == 0
    for table in tables:
        counts = _per_tenant(superuser_conn, table)
        assert acme not in counts, table
        assert counts.get(other.tenant_id) == others[table], table
    tombstones = superuser_conn.execute(
        "SELECT count(*) FROM audit_log WHERE action = 'tenant.purged' AND entity_id = %s", (acme,)
    ).fetchone()
    assert tombstones == (2,)  # purge_tenant 'data' and 'final'


async def test_seed_reset_after_demo_and_login(superuser_conn: Conn, api: Any) -> None:
    """S10 A21b (carried to S10c): the demo seed and real HTTP logins as app_rw, then
    `seed --reset`: the reset goes through purge_tenant past the append-only login audit rows and
    the refresh-token rows, and the reseeded world logs in again."""
    from app.db.seed_ops import SEED_PASSWORD

    assert await _seed("demo") == 0
    login = await api.post(
        "/api/v1/auth/login", json={"email": "owner@acme.test", "password": SEED_PASSWORD}
    )
    assert login.status_code == 200, login.text
    old_refresh = login.json()["refresh_token"]
    rotated = await api.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert rotated.status_code == 200
    failed = await api.post(
        "/api/v1/auth/login", json={"email": "owner@acme.test", "password": "wrong-password"}
    )
    assert failed.status_code == 401
    acme = _ids()["acme"]
    assert _per_tenant(superuser_conn, "refresh_tokens") == {acme: 2}
    assert _per_tenant(superuser_conn, "audit_log").get(acme) == 1  # auth.login
    assert await _seed("demo", reset=True) == 0
    assert _per_tenant(superuser_conn, "refresh_tokens") == {}
    assert _per_tenant(superuser_conn, "audit_log").get(acme) is None
    # the failed login's row has no tenant, so the purge leaves it (a platform row)
    assert (
        _scalar(superuser_conn, "SELECT count(*) FROM audit_log WHERE action = 'auth.login_failed'")
        == 1
    )
    again = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert again.status_code == 401
    relogin = await api.post(
        "/api/v1/auth/login", json={"email": "owner@acme.test", "password": SEED_PASSWORD}
    )
    assert relogin.status_code == 200


async def test_seed_platform_rows_no_tenants_idempotent(superuser_conn: Conn) -> None:
    from app.cli import RULES_SEED_PATH
    from app.db.seed_ops import seed_platform_rows
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        for _ in range(2):
            async with engine.begin() as connection:
                await seed_platform_rows(connection, RULES_SEED_PATH)
            assert _scalar(superuser_conn, "SELECT count(*) FROM category_templates") == 4
            assert _scalar(superuser_conn, "SELECT count(*) FROM rules") == len(_fixture_rule_ids())
            assert _scalar(superuser_conn, "SELECT count(*) FROM tenants") == 0
    finally:
        await engine.dispose()


async def test_seed_reset_then_seed_counts(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    first = _all_counts(superuser_conn)
    assert await _seed("demo", reset=True) == 0
    after = _all_counts(superuser_conn)
    # the reset purges both seed tenants: two `tenant.purged` platform tombstones each (accepted
    # build-up in dev); every other count is back where it was
    assert after.pop("audit_log") == first.pop("audit_log") + 4
    assert after == first


async def test_seed_rules_idempotent(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    revisions = superuser_conn.execute("SELECT id, revision FROM rules ORDER BY id").fetchall()
    assert await _seed("isolation") == 0
    assert (
        superuser_conn.execute("SELECT id, revision FROM rules ORDER BY id").fetchall() == revisions
    )


# ---- T21: the image role ----


def test_seed_role_refused_under_staging(superuser_conn: Conn) -> None:
    before = _all_counts(superuser_conn)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "APP_ROLE": "seed",
        "APP_ENV": "staging",
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "ALEMBIC_DATABASE_URL": os.environ["DATABASE_URL"],
        "JWT_SECRET": NON_COMMITTED_SECRET,
    }
    done = subprocess.run(  # fixed argv, no shell
        [sys.executable, "-m", "app.cli", "seed", "--profile", "demo"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT / "apps/api"),
        check=False,
    )
    assert done.returncode == 3, done.stderr
    assert "seed: refused: APP_ENV=staging" in done.stderr
    assert _all_counts(superuser_conn) == before
