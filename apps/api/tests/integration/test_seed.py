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
    # acme's demo kurta family (S20 T21) adds a style and its 4 children
    assert _per_tenant(superuser_conn, "products") == {ids["acme"]: 9, ids["bharat"]: 4}


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
        ("amazon", "tshirts"),  # S20 T5: the Amazon tshirts mapping
        ("flipkart", "ethnic_wear_kurta_sets"),
        ("flipkart", "home_decor_lamps"),
        ("flipkart", "tshirts"),  # S22 T15: the Flipkart tshirts mapping
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


# ---- S12 T2: the two dictionary rows (PLAN D4, CODE-PLAN CP3) ----

_DICTIONARY_SQL = (
    "SELECT key, storage, owner_levels, input_aliases, is_critical, allowed_sources, "
    "confirm_agent_captures, enabled, revision FROM attribute_definitions "
    "WHERE key IN ('color', 'fabric_type') ORDER BY key"
)


@pytest.mark.parametrize("profile", ["demo", "isolation"])
async def test_seed_attribute_definitions_present(superuser_conn: Conn, profile: str) -> None:
    assert await _seed(profile) == 0
    rows = {r[0]: r for r in superuser_conn.execute(_DICTIONARY_SQL).fetchall()}
    assert set(rows) == {"color", "fabric_type"}
    assert rows["fabric_type"][3] == ["material"]
    assert rows["color"][3] == []
    for key in ("color", "fabric_type"):
        assert rows[key][1] == "attributes"
        assert "standalone" in rows[key][2]
        assert rows[key][7] is True
    assert rows["fabric_type"][4] is True
    assert "ai" not in rows["fabric_type"][5]
    assert rows["fabric_type"][6] is True


async def test_seed_attribute_definitions_idempotent(superuser_conn: Conn) -> None:
    assert await _seed("demo") == 0
    first = superuser_conn.execute(_DICTIONARY_SQL).fetchall()
    assert await _seed("demo") == 0
    assert superuser_conn.execute(_DICTIONARY_SQL).fetchall() == first
    # S13b: the seed syncs the dictionary file, which owns the rows: a hand edit is put back
    superuser_conn.execute("UPDATE attribute_definitions SET enabled = false WHERE key = 'color'")
    try:
        assert await _seed("demo") == 0
        enabled = _scalar(
            superuser_conn, "SELECT enabled FROM attribute_definitions WHERE key = 'color'"
        )
        assert enabled is True
    finally:
        superuser_conn.execute(
            "UPDATE attribute_definitions SET enabled = true WHERE key = 'color'"
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


# ---- S14b T19 (AC 14 seed half; D12): the demo listing ----


def _demo_rows(conn: Conn) -> tuple[int, int, int]:
    from app.db.seed_ops import demo_listing_ids

    listing, version, run = demo_listing_ids()
    return (
        int(_scalar(conn, "SELECT count(*) FROM listings WHERE id = %s", [listing])),
        int(_scalar(conn, "SELECT count(*) FROM listing_versions WHERE id = %s", [version])),
        int(_scalar(conn, "SELECT count(*) FROM validation_runs WHERE id = %s", [run])),
    )


async def test_seed_demo_listing_once(superuser_conn: Conn) -> None:
    from app.db.seed_ops import demo_listing_ids

    assert await _seed("demo") == 0
    assert await _seed("demo") == 0
    assert _demo_rows(superuser_conn) == (1, 1, 1)
    listing, version, _ = demo_listing_ids()
    row = superuser_conn.execute(
        "SELECT l.status, l.current_version_id, l.tenant_id, v.content_json ->> 'title', "
        "v.generated_by_user_id = u.id FROM listings l JOIN listing_versions v ON v.listing_id = l.id "
        "JOIN users u ON u.email = 'operator@acme.test' WHERE l.id = %s",
        [listing],
    ).fetchone()
    assert row is not None
    assert (row[0], row[1], row[2]) == ("created", version, _ids()["acme"])
    assert len(row[3]) == 221 and row[4] is True


async def test_seed_demo_listing_report_from_engine(superuser_conn: Conn) -> None:
    from app.db.seed_ops import DEMO_ATTRIBUTES, demo_listing_content, demo_listing_ids
    from app.repositories.uow import tenant_uow
    from app.services.rules_engine import evaluate
    from app.services.rules_loader import load_rules
    from mkos_schemas.errors import ErrorReport
    from mkos_schemas.listing import CanonicalListing

    assert await _seed("demo") == 0
    acme = _ids()["acme"]
    listing_id, version_id, run_id = demo_listing_ids()
    row = superuser_conn.execute(
        "SELECT source, passed, error_report_json, rules_version, category_template_id, "
        "template_version, facts_digest, listing_version_id, blocking_count FROM validation_runs "
        "WHERE id = %s",
        [run_id],
    ).fetchone()
    assert row is not None
    async with tenant_uow(acme) as uow:
        loaded = await load_rules(
            uow, tenant_id=acme, marketplace="amazon", category="ethnic_wear_kurta_sets"
        )
    # S20 C12: the run pins S9's version 1, read when the listing was seeded; the Amazon kurta
    # mapping (version 2) is promoted after it.
    v1 = superuser_conn.execute(
        "SELECT id, version, attributes FROM category_templates WHERE marketplace = 'amazon' "
        "AND category = 'ethnic_wear_kurta_sets' AND version = 1"
    ).fetchone()
    assert v1 is not None
    from mkos_schemas.jobs import CategoryTemplate

    pinned = CategoryTemplate.model_validate(
        {**v1[2], "marketplace": "amazon", "category": "ethnic_wear_kurta_sets"}
    )
    product = _scalar(superuser_conn, "SELECT product_id FROM listings WHERE id = %s", [listing_id])
    listing = CanonicalListing.model_validate(
        {
            "listing_id": listing_id,
            "product_id": product,
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "content": demo_listing_content(),
            "attributes": dict(DEMO_ATTRIBUTES),
        }
    )
    expected = evaluate(listing, loaded.ruleset, template=pinned)
    assert ErrorReport.model_validate(row[2]) == expected
    assert [e.error_code.value for e in expected.errors] == ["FIELD_TOO_LONG"]
    assert (row[0], row[1], row[3]) == ("rules_engine", False, loaded.ruleset.rules_version)
    assert (row[4], row[5]) == (v1[0], 1)
    assert row[6] is not None and row[7] == version_id and row[8] == 1


async def test_seed_demo_listing_absent_in_isolation_profile(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    assert _demo_rows(superuser_conn) == (0, 0, 0)


# ---- S20 T5 (C12): the Amazon mappings, as new template versions ----


def _templates(conn: Conn, category: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT version, is_current, product_type, attributes FROM category_templates "
        "WHERE marketplace = 'amazon' AND category = %s ORDER BY version",
        [category],
    ).fetchall()


def _promotions(conn: Conn, category: str) -> int:
    return int(
        _scalar(
            conn,
            "SELECT count(*) FROM audit_log WHERE action = 'category_template.promoted' "
            "AND tenant_id IS NULL AND after ->> 'category' = %s "
            "AND after ->> 'marketplace' = 'amazon'",
            [category],
        )
    )


def _kurta_001_pin(conn: Conn) -> int:
    from app.db.seed_ops import demo_listing_ids

    _listing, version, run = demo_listing_ids()
    pinned = conn.execute(
        "SELECT t.version FROM listing_versions v JOIN category_templates t "
        "ON t.id = v.category_template_id WHERE v.id = %s",
        [version],
    ).fetchone()
    run_pin = _scalar(conn, "SELECT template_version FROM validation_runs WHERE id = %s", [run])
    assert pinned is not None and pinned[0] == run_pin
    return int(run_pin)


async def test_seed_amazon_kurta_v2_promoted_once(superuser_conn: Conn) -> None:
    from app.db.seed_ops import _template_rows

    assert await _seed("demo") == 0
    first = _templates(superuser_conn, "ethnic_wear_kurta_sets")
    assert await _seed("demo") == 0
    rows = _templates(superuser_conn, "ethnic_wear_kurta_sets")
    assert rows == first
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, False, "KURTA"), (2, True, "KURTA")]
    (v1_seed,) = [
        t
        for t in _template_rows()
        if (t["marketplace"], t["category"]) == ("amazon", "ethnic_wear_kurta_sets")
    ]
    assert rows[0][3] == v1_seed["attributes"]  # S9's v1 content unchanged
    assert rows[1][3]["specs"]["color"]["marketplace_field"] == "color"
    assert _promotions(superuser_conn, "ethnic_wear_kurta_sets") == 1


async def test_seed_amazon_tshirts_v1_current(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    rows = _templates(superuser_conn, "tshirts")
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, True, "SHIRT")]
    assert _promotions(superuser_conn, "tshirts") == 1


async def test_seed_on_existing_v1_db_ok(
    superuser_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database seeded before S20 (S9's v1 rows, KURTA-001 and its validation run) seeds
    again without a guard error, and KURTA-001 keeps its v1 pin."""
    from app.db import seed_ops
    from app.db.platform_loads import TemplateCounts

    async def before_s20(connection: Any) -> TemplateCounts:
        return TemplateCounts(inserted=0, unchanged=0, promoted=0)

    async def no_family(session: Any) -> None:  # S20 T21's family came with the mappings
        return None

    with monkeypatch.context() as patch:
        patch.setattr(seed_ops, "seed_amazon_mappings", before_s20)
        patch.setattr(seed_ops, "seed_flipkart_mappings", before_s20)  # S22 T15 likewise
        patch.setattr(seed_ops, "seed_demo_family", no_family)
        patch.setattr(seed_ops, "seed_demo_flipkart", no_family)
        assert await _seed("demo") == 0
    assert [r[0] for r in _templates(superuser_conn, "ethnic_wear_kurta_sets")] == [1]
    assert _kurta_001_pin(superuser_conn) == 1
    assert await _seed("demo") == 0
    assert [(r[0], r[1]) for r in _templates(superuser_conn, "ethnic_wear_kurta_sets")] == [
        (1, False),
        (2, True),
    ]
    assert _kurta_001_pin(superuser_conn) == 1


async def test_promote_template_two_updates_one_tx(superuser_conn: Conn) -> None:
    from app.cli import RULES_SEED_PATH
    from app.db.seed_ops import seed_amazon_mappings, seed_platform_rows
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await seed_platform_rows(connection, RULES_SEED_PATH)
        with pytest.raises(RuntimeError, match="between"):
            async with engine.begin() as connection:
                await seed_amazon_mappings(connection)
                raise RuntimeError("a failure between the promotion and the commit")
    finally:
        await engine.dispose()
    assert [(r[0], r[1]) for r in _templates(superuser_conn, "ethnic_wear_kurta_sets")] == [
        (1, True)
    ]
    assert _templates(superuser_conn, "tshirts") == []
    currents = _scalar(
        superuser_conn,
        "SELECT max(n) FROM (SELECT count(*) AS n FROM category_templates WHERE is_current "
        "GROUP BY marketplace, category) c",
    )
    assert currents == 1


async def test_seed_calls_platform_load_once(
    superuser_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S20 T5b (C18): `seed` reaches the Amazon mappings and the error map through the one
    `platform_load` path, once per run."""
    from app.db import seed_ops

    calls: list[int] = []
    real = seed_ops.platform_load

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(seed_ops, "platform_load", spy)
    assert await _seed("demo") == 0
    assert await _seed("demo") == 0
    assert calls == [1, 1]
    assert _promotions(superuser_conn, "ethnic_wear_kurta_sets") == 1
    # S20's 4 Amazon rows, S21's FAKE-QUOTA row and S22's 4 Flipkart rows
    assert _scalar(superuser_conn, "SELECT count(*) FROM marketplace_error_map") == 9


# ---- S20 T21 (AC17; C12): the demo kurta family ----


async def _preview(listing_id: UUID) -> Any:
    from app.core.rbac import Principal
    from app.services.listing_preview import preview_listing

    acme = _ids()["acme"]
    principal = Principal(UUID(int=1), acme, "admin", "admin@acme.test", "seed-test")
    return await preview_listing(principal, listing_id)


async def test_seed_demo_family_once(superuser_conn: Conn) -> None:
    from app.db.seed_ops import DEMO_FAMILY_SKU, demo_family_ids

    assert await _seed("demo") == 0
    assert await _seed("demo") == 0
    style, listing, version = demo_family_ids()
    rows = superuser_conn.execute(
        "SELECT sku, variant_attributes, parent_product_id IS NULL FROM products "
        "WHERE id = %s OR parent_product_id = %s ORDER BY sku",
        [style, style],
    ).fetchall()
    assert [r[0] for r in rows] == [
        DEMO_FAMILY_SKU,
        f"{DEMO_FAMILY_SKU}-M-MAROON",
        f"{DEMO_FAMILY_SKU}-M-NAVY-BLUE",
        f"{DEMO_FAMILY_SKU}-S-MAROON",
        f"{DEMO_FAMILY_SKU}-S-NAVY-BLUE",
    ]
    assert {r[1]["color"] for r in rows[1:]} == {"navy blue", "maroon"}
    counts = (
        _scalar(superuser_conn, "SELECT count(*) FROM listings WHERE id = %s", [listing]),
        _scalar(superuser_conn, "SELECT count(*) FROM listing_versions WHERE id = %s", [version]),
        _scalar(
            superuser_conn,
            "SELECT count(*) FROM variant_matrix_cells WHERE style_product_id = %s "
            "AND availability = 'available'",
            [style],
        ),
    )
    assert counts == (1, 1, 4)
    current = _scalar(
        superuser_conn, "SELECT current_version_id FROM listings WHERE id = %s", [listing]
    )
    assert current == version


async def test_seed_demo_family_pins_kurta_v2(superuser_conn: Conn) -> None:
    from app.db.seed_ops import demo_family_ids

    assert await _seed("demo") == 0
    _style, _listing, version = demo_family_ids()
    row = superuser_conn.execute(
        "SELECT t.version, t.is_current, t.product_type FROM listing_versions v "
        "JOIN category_templates t ON t.id = v.category_template_id WHERE v.id = %s",
        [version],
    ).fetchone()
    assert row == (2, True, "KURTA")


async def test_seed_demo_family_preview_has_no_errors(superuser_conn: Conn) -> None:
    from app.db.seed_ops import DEMO_FAMILY_SKU, demo_family_ids

    assert await _seed("demo") == 0
    preview = await _preview(demo_family_ids()[1])
    assert preview.product_type == "KURTA"
    assert [i.item_role for i in preview.items] == ["parent"] + ["child"] * 4
    assert preview.items[0].seller_sku == DEMO_FAMILY_SKU
    assert preview.report.errors == []


async def test_seed_demo_listing_stays_on_v1(superuser_conn: Conn) -> None:
    from app.db.seed_ops import demo_listing_ids

    assert await _seed("demo") == 0
    assert await _seed("demo") == 0
    assert _kurta_001_pin(superuser_conn) == 1
    preview = await _preview(demo_listing_ids()[0])
    assert preview.product_type is None and len(preview.items) == 1
    assert [e.error_code.value for e in preview.report.errors] == ["CATEGORY_NOT_MAPPED"]


# ---- S22 T15 (D7; R3): the Flipkart mappings and acme's Flipkart demo listings ----


def _fk_templates(conn: Conn, category: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT version, is_current, product_type, attributes FROM category_templates "
        "WHERE marketplace = 'flipkart' AND category = %s ORDER BY version",
        [category],
    ).fetchall()


def _fk_promotions(conn: Conn, category: str) -> int:
    return int(
        _scalar(
            conn,
            "SELECT count(*) FROM audit_log WHERE action = 'category_template.promoted' "
            "AND tenant_id IS NULL AND after ->> 'category' = %s "
            "AND after ->> 'marketplace' = 'flipkart'",
            [category],
        )
    )


async def test_seed_flipkart_kurta_v2_promoted_once(superuser_conn: Conn) -> None:
    from app.db.seed_ops import _template_rows

    assert await _seed("demo") == 0
    first = _fk_templates(superuser_conn, "ethnic_wear_kurta_sets")
    assert await _seed("demo") == 0
    rows = _fk_templates(superuser_conn, "ethnic_wear_kurta_sets")
    assert rows == first
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, False, "KURTA"), (2, True, "kurta")]
    (v1_seed,) = [
        t
        for t in _template_rows()
        if (t["marketplace"], t["category"]) == ("flipkart", "ethnic_wear_kurta_sets")
    ]
    assert rows[0][3] == v1_seed["attributes"]
    assert rows[1][3]["specs"]["color"]["marketplace_field"] == "Colour"
    assert _fk_promotions(superuser_conn, "ethnic_wear_kurta_sets") == 1
    lamps = _fk_templates(superuser_conn, "home_decor_lamps")
    assert [(r[0], r[1]) for r in lamps] == [(1, False), (2, True)]  # A2: the lamp mapping


async def test_seed_lamp_mappings_v2_promoted_once(superuser_conn: Conn) -> None:
    """A2: `home_decor_lamps` gets a mapped version 2 on both marketplaces, promoted over S9's
    empty version 1 (left as it was); a second seed writes and audits nothing."""
    from app.db.seed_ops import _template_rows

    assert await _seed("demo") == 0
    first = (
        _templates(superuser_conn, "home_decor_lamps"),
        _fk_templates(superuser_conn, "home_decor_lamps"),
    )
    assert await _seed("demo") == 0
    amazon = _templates(superuser_conn, "home_decor_lamps")
    flipkart = _fk_templates(superuser_conn, "home_decor_lamps")
    assert (amazon, flipkart) == first
    assert [(r[0], r[1], r[2]) for r in amazon] == [(1, False, "LAMP"), (2, True, "LAMP")]
    assert [(r[0], r[1], r[2]) for r in flipkart] == [(1, False, "LAMP"), (2, True, "table_lamp")]
    v1 = {
        t["marketplace"]: t["attributes"]
        for t in _template_rows()
        if t["category"] == "home_decor_lamps"
    }
    assert amazon[0][3] == v1["amazon"] and flipkart[0][3] == v1["flipkart"]
    for rows in (amazon, flipkart):
        assert rows[1][3]["required_attributes"] == ["color", "product_dimensions"]
    assert _promotions(superuser_conn, "home_decor_lamps") == 1
    assert _fk_promotions(superuser_conn, "home_decor_lamps") == 1


async def test_seed_flipkart_tshirts_v1_current(superuser_conn: Conn) -> None:
    assert await _seed("isolation") == 0
    rows = _fk_templates(superuser_conn, "tshirts")
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, True, "t_shirt")]


async def test_seed_demo_flipkart_once(superuser_conn: Conn) -> None:
    from app.db.seed_ops import demo_flipkart_ids

    assert await _seed("demo") == 0
    assert await _seed("demo") == 0
    for listing, version in demo_flipkart_ids().values():
        row = superuser_conn.execute(
            "SELECT l.marketplace, l.current_version_id, t.marketplace, t.version "
            "FROM listings l JOIN listing_versions v ON v.id = l.current_version_id "
            "JOIN category_templates t ON t.id = v.category_template_id WHERE l.id = %s",
            [listing],
        ).fetchone()
        assert row == ("flipkart", version, "flipkart", 2)
    count = _scalar(superuser_conn, "SELECT count(*) FROM listings WHERE marketplace = 'flipkart'")
    assert count == 2


async def test_seed_demo_flipkart_family_preview_no_errors(superuser_conn: Conn) -> None:
    from app.db.seed_ops import DEMO_FAMILY_SKU, DEMO_LISTING_SKU, demo_flipkart_ids

    assert await _seed("demo") == 0
    ids = demo_flipkart_ids()
    family = await _preview(ids[DEMO_FAMILY_SKU][0])
    assert family.product_type == "kurta"
    assert [i.item_role for i in family.items] == ["child"] * 4
    assert DEMO_FAMILY_SKU not in {i.seller_sku for i in family.items}
    assert family.report.errors == []
    for item in family.items:
        assert item.payload["attributes"]["style_code"] == [{"value": DEMO_FAMILY_SKU}]
    standalone = await _preview(ids[DEMO_LISTING_SKU][0])
    assert [i.item_role for i in standalone.items] == ["standalone"]
    assert standalone.report.errors == [], standalone.report.errors
    assert standalone.items[0].payload["attributes"]["Colour"] == [{"value": "Indigo"}]
