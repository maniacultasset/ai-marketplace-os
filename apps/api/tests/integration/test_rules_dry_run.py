"""S14b T12 (AC 9; Done-when 3; D7, D8, C7): `POST /api/v1/rules/dry-run` runs the Rules Engine
on a draft listing for the JWT tenant and saves nothing."""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import bad_bearer, bearer_for
from .seam_support import CATEGORY, Conn, add_rule, banned

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
DRY_RUN = "/api/v1/rules/dry-run"
GOLDEN = Path(__file__).resolve().parent / "golden" / "rules_dry_run_221.json"
LISTING_ID = "8d0f6f4e-14b2-4f8e-9d1a-221221221221"
PRODUCT_ID = "8d0f6f4e-14b2-4f8e-9d1a-000000000001"
UNIT = "Cotton Kurta Set for Women with Straight Fit and Round Neck "
TITLE_221 = (UNIT * 4)[:220] + "."
TITLE_200 = (UNIT * 4)[:199] + "."


@pytest.fixture
async def dry_world(products_world: World, auth_db: Conn) -> World:
    """The products world plus the platform seed rows (templates, the seeded rules), as `make
    seed` writes them on the migrator connection."""
    import os

    from app.cli import RULES_SEED_PATH
    from app.db.seed_ops import seed_platform_rows
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["ALEMBIC_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await seed_platform_rows(connection, RULES_SEED_PATH)
    finally:
        await engine.dispose()
    return products_world


def draft(title: str = TITLE_200, **over: Any) -> dict[str, Any]:
    listing: dict[str, Any] = {
        "listing_id": LISTING_ID,
        "product_id": PRODUCT_ID,
        "marketplace": "amazon",
        "category": CATEGORY,
        "content": {
            "title": title,
            "bullets": ["Soft cotton fabric"],
            "description": "A cotton kurta set.",
        },
        "attributes": {"color": "indigo", "fabric_type": "cotton"},
    }
    listing.update(over)
    return {"listing": listing}


def _as(world: World, role: str = "operator", slug: str = "acme") -> dict[str, str]:
    return bearer_for(world, f"{role}@{slug}.test")


def _counts(conn: Conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relname <> "
        "'rate_limit_counters' ORDER BY 1"
    ).fetchall()
    out: dict[str, int] = {}
    for (name,) in rows:
        row = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()  # noqa: S608
        assert row is not None
        out[name] = int(row[0])
    return out


def _codes(body: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["error_code"], e["field"]) for e in body["report"]["errors"]]


# ---- success ----


@pytest.mark.endpoint("rules_dry_run", case="success")
async def test_rules_dry_run_221_golden(api: httpx.AsyncClient, dry_world: World) -> None:
    response = await api.post(DRY_RUN, json=draft(TITLE_221), headers=_as(dry_world))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] == json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert body["passed"] is False and body["source"] == "rules_engine"
    assert body["tenant_id"] == str(dry_world.tenant("acme"))
    assert body["listing_id"] == LISTING_ID
    assert body["validation_run_id"] is None
    assert isinstance(body["rules_version"], int) and body["rules_version"] > 0
    (item,) = body["report"]["errors"]
    assert (item["error_code"], item["field"], item["rule_id"]) == (
        "FIELD_TOO_LONG",
        "content.title",
        "5eed0000-0000-4000-8000-000100010001",
    )


@pytest.mark.endpoint("rules_dry_run", case="success")
async def test_rules_dry_run_200_passes(api: httpx.AsyncClient, dry_world: World) -> None:
    response = await api.post(DRY_RUN, json=draft(TITLE_200), headers=_as(dry_world, "owner"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["passed"] is True and body["report"]["errors"] == []


async def test_rules_dry_run_persists_nothing(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn
) -> None:
    before = _counts(auth_db)
    assert "procrastinate_jobs" in before and "validation_runs" in before
    response = await api.post(DRY_RUN, json=draft(TITLE_221), headers=_as(dry_world))
    assert response.status_code == 200
    assert _counts(auth_db) == before
    assert before["job_runs"] == 0


# ---- validation (422) ----


@pytest.mark.endpoint("rules_dry_run", case="validation")
@pytest.mark.parametrize(
    "case",
    [
        "bad_listing",
        "extra_key",
        "unknown_pair",
        "unknown_category_with_null_category_rules",
        "nan_attr",
        "tenant_id_top_level",
    ],
)
async def test_rules_dry_run_validation(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn, case: str
) -> None:
    body: Any
    if case == "bad_listing":
        body = draft(content={"title": "", "bullets": [], "description": ""})
    elif case == "extra_key":
        body = {**draft(), "unexpected": True}
    elif case == "unknown_pair":
        body = draft(marketplace="flipkart", category="ethnic_wear_jackets")
    elif case == "unknown_category_with_null_category_rules":
        from . import factories as f

        add_rule(
            auth_db,
            banned(["zzz"], policy_ref=f.POLICY_REF),
            category=None,
        )
        body = draft(category="home_decor_rugs")
    elif case == "nan_attr":
        content = json.dumps(draft()).replace('"indigo"', "NaN")
        response = await api.post(
            DRY_RUN,
            content=content,
            headers={**_as(dry_world), "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "VALIDATION_FAILED"
        return
    else:
        body = {**draft(), "tenant_id": str(dry_world.tenant("bharat"))}
    response = await api.post(DRY_RUN, json=body, headers=_as(dry_world))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    if case.startswith("unknown"):
        details = response.json()["error"]["details"]
        assert details == [{"loc": ["body", "listing", "category"], "msg": details[0]["msg"]}]


# ---- body caps (413) ----


@pytest.fixture
async def small_cap_api(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", "512")
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=create_app(), client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    get_settings.cache_clear()


async def test_rules_dry_run_body_over_cap_413(
    small_cap_api: httpx.AsyncClient, dry_world: World
) -> None:
    response = await small_cap_api.post(DRY_RUN, json=draft(TITLE_221), headers=_as(dry_world))
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"


async def test_rules_dry_run_body_cap_route_level(api: httpx.AsyncClient, dry_world: World) -> None:
    from app.api.v1.routers.rules import DRY_RUN_MAX_BODY_BYTES

    padded = json.dumps(draft()).encode()
    payload = padded[:-1] + b" " * (DRY_RUN_MAX_BODY_BYTES + 1 - len(padded)) + padded[-1:]
    assert len(payload) == DRY_RUN_MAX_BODY_BYTES + 1
    headers = {**_as(dry_world), "Content-Type": "application/json"}
    refused = await api.post(DRY_RUN, content=payload, headers=headers)
    assert refused.status_code == 413
    assert refused.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"
    # the same size to another route is not refused by the path cap (it reaches validation)
    other = await api.post("/api/v1/products", content=payload, headers=headers)
    assert other.status_code != 413


def test_dry_run_cap_fits_max_listing() -> None:
    from app.api.v1.routers.rules import DRY_RUN_MAX_BODY_BYTES
    from app.api.v1.schemas.listings import DryRunRequest

    listing = {
        "listing_id": LISTING_ID,
        "product_id": PRODUCT_ID,
        "marketplace": "amazon",
        "category": "c" * 64,
        "content": {
            "title": "t" * 500,
            "bullets": ["b" * 1000] * 10,
            "description": "d" * 5000,
        },
        "keywords": {"search_terms": ["s" * 256] * 50, "hidden_terms": ["h" * 256] * 50},
        "attributes": {f"{'a' * 60}{n:04d}": ["v" * 256] * 2 for n in range(128)},
        "images": [
            {"image_id": str(uuid.UUID(int=n + 1)), "slot": "secondary", "position": n}
            for n in range(20)
        ],
    }
    body = DryRunRequest.model_validate({"listing": listing}).model_dump_json().encode()
    assert len(body) <= DRY_RUN_MAX_BODY_BYTES, len(body)


# ---- the event loop (C7) ----


async def test_rules_dry_run_does_not_block_event_loop(
    api: httpx.AsyncClient, dry_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import rules_dry_run
    from app.services.rules_engine import evaluate as real

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(rules_dry_run, "evaluate", slow)
    launched = time.monotonic()
    task = asyncio.create_task(api.post(DRY_RUN, json=draft(), headers=_as(dry_world)))
    await asyncio.sleep(0.15)  # the dry-run is inside `evaluate` by now
    started = time.monotonic()
    health = await api.get("/healthz")
    answered = time.monotonic()
    assert health.status_code == 200
    # on the loop, the 0.5 s `evaluate` would hold both the sleep and /healthz behind it
    assert answered - started < 0.1, answered - started
    assert answered - launched < 0.4, answered - launched
    assert not task.done()
    assert (await task).status_code == 200

    started = time.monotonic()
    results = await asyncio.gather(
        *(api.post(DRY_RUN, json=draft(), headers=_as(dry_world)) for _ in range(4))
    )
    assert [r.status_code for r in results] == [200] * 4
    assert time.monotonic() - started >= 1.0  # two at a time (the limiter of 2)


# ---- auth ----


@pytest.mark.endpoint("rules_dry_run", case="missing_auth")
async def test_rules_dry_run_missing_auth(api: httpx.AsyncClient, dry_world: World) -> None:
    response = await api.post(DRY_RUN, json=draft())
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


async def _invalid(api: httpx.AsyncClient, world: World, variant: str) -> None:
    response = await api.post(DRY_RUN, json=draft(), headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("rules_dry_run", case="invalid_token", variant="expired")
async def test_rules_dry_run_invalid_token_expired(
    api: httpx.AsyncClient, dry_world: World
) -> None:
    await _invalid(api, dry_world, "expired")


@pytest.mark.endpoint("rules_dry_run", case="invalid_token", variant="bad_signature")
async def test_rules_dry_run_invalid_token_bad_signature(
    api: httpx.AsyncClient, dry_world: World
) -> None:
    await _invalid(api, dry_world, "bad_signature")


@pytest.mark.endpoint("rules_dry_run", case="invalid_token", variant="wrong_typ")
async def test_rules_dry_run_invalid_token_wrong_typ(
    api: httpx.AsyncClient, dry_world: World
) -> None:
    await _invalid(api, dry_world, "wrong_typ")


@pytest.mark.endpoint("rules_dry_run", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_rules_dry_run_wrong_role(
    api: httpx.AsyncClient, dry_world: World, role: str
) -> None:
    response = await api.post(DRY_RUN, json=draft(), headers=_as(dry_world, role))
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    for allowed in ("owner", "operator"):
        ok = await api.post(DRY_RUN, json=draft(), headers=_as(dry_world, allowed))
        assert ok.status_code == 200


@pytest.mark.endpoint("rules_dry_run", case="wrong_tenant")
async def test_rules_dry_run_wrong_tenant(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn
) -> None:
    add_rule(auth_db, banned(["handloom"]), tenant_id=dry_world.tenant("acme"))
    title = "Handloom cotton kurta set"
    acme = await api.post(DRY_RUN, json=draft(title), headers=_as(dry_world))
    bharat = await api.post(DRY_RUN, json=draft(title), headers=_as(dry_world, slug="bharat"))
    assert ("BANNED_TERM", "content.title") in _codes(acme.json())
    assert bharat.json()["passed"] is True
    assert bharat.json()["tenant_id"] == str(dry_world.tenant("bharat"))
    # a tenant id inside the listing is dropped, never trusted
    smuggled = draft(title)
    smuggled["listing"]["tenant_id"] = str(dry_world.tenant("acme"))
    again = await api.post(DRY_RUN, json=smuggled, headers=_as(dry_world, slug="bharat"))
    assert again.status_code == 200 and again.json()["passed"] is True


# ---- not in cases(op), proven unmarked ----


async def test_rules_dry_run_na_unknown_listing_id_still_200(
    api: httpx.AsyncClient, dry_world: World
) -> None:
    body = draft(listing_id=str(uuid.uuid4()))
    assert (await api.post(DRY_RUN, json=body, headers=_as(dry_world))).status_code == 200
    without = draft()
    del without["listing"]["listing_id"]
    response = await api.post(DRY_RUN, json=without, headers=_as(dry_world))
    assert response.status_code == 200
    assert response.json()["listing_id"] == str(uuid.UUID(int=0))


async def test_rules_dry_run_na_concurrent_identical(
    api: httpx.AsyncClient, dry_world: World
) -> None:
    results = await asyncio.gather(
        *(api.post(DRY_RUN, json=draft(TITLE_221), headers=_as(dry_world)) for _ in range(5))
    )
    assert [r.status_code for r in results] == [200] * 5
    assert len({json.dumps(r.json()["report"], sort_keys=True) for r in results}) == 1


async def test_rules_dry_run_idempotency_key_ignored(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn
) -> None:
    headers = {**_as(dry_world), "Idempotency-Key": str(uuid.uuid4())}
    for _ in range(2):
        assert (await api.post(DRY_RUN, json=draft(), headers=headers)).status_code == 200
    assert auth_db.execute("SELECT count(*) FROM idempotency_keys").fetchone() == (0,)


async def test_rules_dry_run_product_conditions_absent(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn
) -> None:
    from . import factories as f

    body = {
        "type": "conditional_required",
        "attribute": "size",
        "when": [{"path": "product.department", "op": "present"}],
        "message": "Size is required.",
        "policy_ref": f.POLICY_REF,
    }
    add_rule(auth_db, body, field="attributes.size", subject="size")
    response = await api.post(DRY_RUN, json=draft(), headers=_as(dry_world))
    assert response.status_code == 200
    assert ("MISSING_ATTRIBUTE", "attributes.size") not in _codes(response.json())


async def test_rules_dry_run_rate_limited_429(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn
) -> None:
    assert (await api.post(DRY_RUN, json=draft(), headers=_as(dry_world))).status_code == 200
    auth_db.execute("UPDATE rate_limit_counters SET n = 1000")
    limited = await api.post(DRY_RUN, json=draft(), headers=_as(dry_world))
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "RATE_LIMITED"


async def test_rules_dry_run_unicode_title(api: httpx.AsyncClient, dry_world: World) -> None:
    verdicts = []
    for count in (99, 101):
        base = "क़" * count  # NFC keeps U+0915 U+093C (a composition exclusion): 2 per glyph
        forms = [unicodedata.normalize(form, base) for form in ("NFC", "NFD")]
        answers = [
            (await api.post(DRY_RUN, json=draft(text), headers=_as(dry_world))).json()["passed"]
            for text in forms
        ]
        assert answers[0] == answers[1]
        verdicts.append(answers[0])
    assert verdicts == [True, False]


async def test_rules_dry_run_rule_load_error_is_500(
    api: httpx.AsyncClient, dry_world: World, auth_db: Conn
) -> None:
    from .seam_support import pattern

    add_rule(auth_db, pattern(r"\s*\s*x"), subject="sku_shape")
    response = await api.post(DRY_RUN, json=draft(), headers=_as(dry_world))
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "INTERNAL_ERROR"
    assert "sku_shape" not in response.text
