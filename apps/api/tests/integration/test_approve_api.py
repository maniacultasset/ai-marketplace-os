"""S19 T8 (AC 1; inv 1, 7; DATABASE.md §7.1): `POST /api/v1/listings/approve`, the happy path,
the per-item order of checks, and the request-level case matrix."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import bad_bearer, bearer_for
from .review_support import (
    OPERATOR,
    REVIEWER,
    audits,
    decisions,
    edited,
    generated,
    item,
    only_item,
    post_approve,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/listings/approve"
APP = Path(__file__).resolve().parents[2] / "app"


@pytest.mark.endpoint("listings_approve", case="success")
async def test_approve_happy(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    result = only_item(response)
    assert result == {
        "listing_id": str(made.listing_id),
        "http_status": 200,
        "listing_status": "approved",
        "job_id": None,
        "error": None,
    }
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["last_event"], row["approved_version_id"]) == (
        "approved",
        "approve",
        made.version_id,
    )
    version = gen_world.conn.execute(
        "SELECT approved_by_user_id, approved_at IS NOT NULL FROM listing_versions WHERE id = %s",
        [made.version_id],
    ).fetchone()
    assert version == (gen_world.user(REVIEWER), True)
    batch_id = uuid.UUID(response.json()["batch_id"])
    decision = gen_world.conn.execute(
        "SELECT batch_id, facts_digest, decided_by_user_id FROM review_decisions "
        "WHERE listing_id = %s AND decision = 'approve'",
        [made.listing_id],
    ).fetchone()
    run_digest = gen_world.scalar(
        "SELECT facts_digest FROM validation_runs WHERE listing_version_id = %s "
        "AND source = 'rules_engine' ORDER BY created_at DESC LIMIT 1",
        [made.version_id],
    )
    assert decision == (batch_id, run_digest, gen_world.user(REVIEWER))
    batch = gen_world.conn.execute(
        "SELECT kind, item_count, created_by_user_id FROM batches WHERE id = %s", [batch_id]
    ).fetchone()
    assert batch == ("approve", 1, gen_world.user(REVIEWER))
    assert audits(gen_world, made.listing_id, "listing.approved") == 1


async def test_approve_never_publishes(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert (
        gen_world.count(
            "job_runs", "listing_id = %s AND kind = 'listing_publish'", [made.listing_id]
        )
        == 0
    )
    assert gen_world.listing(made.listing_id)["marketplace_state"] == "not_submitted"


async def test_review_paths_write_no_agent_runs(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made, other = await generated(gen_world), await generated(gen_world)
    before = gen_world.count("agent_runs")
    only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    reject = item(other, "reject", reason_code="poor_quality", feedback="Too generic.")
    only_item(await post_approve(api, gen_world, REVIEWER, [reject]))
    assert gen_world.count("agent_runs") == before


async def test_approve_item_error_is_error_report(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    stale = {**item(made), "listing_version_id": str(uuid.uuid4())}
    result = only_item(await post_approve(api, gen_world, REVIEWER, [stale]))
    assert result["http_status"] == 409
    report = result["error"]
    assert report["status"] == "failed" and report["error_code"] == "STALE_VERSION"
    assert report["errors"][0]["error_code"] == "STALE_VERSION"
    assert report["errors"][0]["field"] == "listing"


@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        ("paused", "LISTING_PAUSED"),
        ("archived", "LISTING_ARCHIVED"),
        ("generating", "LISTING_BUSY"),
        ("publishing", "LISTING_BUSY"),
        ("created", "INVALID_STATUS_TRANSITION"),
        ("blocked", "INVALID_STATUS_TRANSITION"),
    ],
)
async def test_approve_status_409(
    api: httpx.AsyncClient, gen_world: GenWorld, arrange: str, code: str
) -> None:
    made = await generated(gen_world)
    if arrange in ("paused", "archived"):
        with gen_world.conn.transaction():
            gen_world.conn.execute("SET LOCAL session_replication_role = replica")
            gen_world.conn.execute(
                f"UPDATE listings SET {arrange}_at = now() WHERE id = %s",  # noqa: S608
                [made.listing_id],
            )
    else:
        set_status(gen_world, made.listing_id, arrange)
    result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == (409, code)
    assert decisions(gen_world, made.listing_id) == 0
    assert gen_world.listing(made.listing_id)["approved_version_id"] is None


async def test_approve_already_approved_409(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """CP-D4: a version approved once is never approved again (revalidate voided it)."""
    made = await generated(gen_world)
    only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    set_status(gen_world, made.listing_id, "needs_review", None)
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE listings SET approved_version_id = NULL WHERE id = %s", [made.listing_id]
        )
    result = only_item(await post_approve(api, gen_world, "owner@acme.test", [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == (
        409,
        "INVALID_STATUS_TRANSITION",
    )
    assert decisions(gen_world, made.listing_id) == 1


def test_review_modules_import_no_adapter() -> None:
    for name in ("review.py", "maker_checker.py", "listing_reads.py"):
        tree = ast.parse((APP / "services" / name).read_text(encoding="utf-8"))
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not {m for m in imported if m.startswith(("app.adapters", "mkos_pipeline"))}, name


# ---- the request-level case matrix ----


@pytest.mark.endpoint("listings_approve", case="validation")
@pytest.mark.parametrize(
    "variant",
    ["no_items", "201_items", "reviewer_id", "dup_ids", "bad_reason", "c0_feedback", "no_key"],
)
async def test_approve_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    lid, vid = str(uuid.uuid4()), str(uuid.uuid4())
    one = {"listing_id": lid, "listing_version_id": vid, "decision": "approve"}
    body: dict[str, object] = {"items": [one]}
    headers = {**bearer_for(gen_world, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    if variant == "no_items":
        body = {"items": []}
    elif variant == "201_items":
        body = {"items": [{**one, "listing_id": str(uuid.uuid4())} for _ in range(201)]}
    elif variant == "reviewer_id":
        body = {"items": [{**one, "reviewer_id": str(uuid.uuid4())}]}
    elif variant == "dup_ids":
        body = {"items": [one, one]}
    elif variant == "bad_reason":
        body = {"items": [{**one, "decision": "reject", "reason_code": "meh", "feedback": "x"}]}
    elif variant == "c0_feedback":
        bad = {**one, "decision": "reject", "reason_code": "other", "feedback": "a\x00b"}
        body = {"items": [bad]}
    else:
        headers.pop("Idempotency-Key")
    response = await api.post(URL, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert gen_world.count("batches") == 0


@pytest.mark.endpoint("listings_approve", case="missing_auth")
async def test_approve_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(URL, headers={"Idempotency-Key": str(uuid.uuid4())}, json={})
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    headers = {**bad_bearer(world, variant, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(URL, headers=headers, json={"items": []})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_approve", case="invalid_token", variant="expired")
async def test_approve_invalid_token_expired(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("listings_approve", case="invalid_token", variant="bad_signature")
async def test_approve_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("listings_approve", case="invalid_token", variant="wrong_typ")
async def test_approve_invalid_token_wrong_typ(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("listings_approve", case="wrong_role")
@pytest.mark.parametrize("email", [OPERATOR, "admin@acme.test"])
async def test_approve_wrong_role_403(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    made = await generated(gen_world)
    response = await post_approve(api, gen_world, email, [item(made)])
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert gen_world.listing(made.listing_id)["status"] == "validated"


@pytest.mark.endpoint("listings_approve", case="wrong_tenant")
async def test_approve_wrong_tenant_item_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    foreign = only_item(await post_approve(api, gen_world, "reviewer@bharat.test", [item(made)]))
    random = only_item(
        await post_approve(
            api,
            gen_world,
            "reviewer@bharat.test",
            [{**item(made), "listing_id": str(uuid.uuid4())}],
        )
    )
    assert foreign["http_status"] == random["http_status"] == 404
    assert foreign["listing_status"] is None
    assert foreign["error"]["errors"] == random["error"]["errors"]
    assert gen_world.listing(made.listing_id)["status"] == "validated"


@pytest.mark.endpoint("listings_approve", case="not_found")
async def test_approve_random_listing_item_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    body = {**item(made), "listing_id": str(uuid.uuid4())}
    result = only_item(await post_approve(api, gen_world, REVIEWER, [body]))
    assert (result["http_status"], result["error"]["error_code"]) == (404, "NOT_FOUND")


@pytest.mark.endpoint("listings_approve", case="conflict")
async def test_approve_key_reuse_409(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    a, b = await generated(gen_world), await generated(gen_world)
    key = str(uuid.uuid4())
    first = await post_approve(api, gen_world, REVIEWER, [item(a)], key=key)
    assert first.status_code == 200
    other = await post_approve(api, gen_world, REVIEWER, [item(b)], key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert gen_world.listing(b.listing_id)["status"] == "validated"


@pytest.mark.endpoint("listings_approve", case="maker_checker", variant="generator_approves")
async def test_api_maker_checker_generator_matrix(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, "owner@acme.test")
    result = only_item(await post_approve(api, gen_world, "owner@acme.test", [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == (
        403,
        "MAKER_CHECKER_VIOLATION",
    )
    assert audits(gen_world, made.listing_id, "listing.approve_denied") == 1


@pytest.mark.endpoint("listings_approve", case="maker_checker", variant="editor_approves")
async def test_api_maker_checker_editor_matrix(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await edited(api, gen_world, await generated(gen_world), "owner@acme.test")
    result = only_item(await post_approve(api, gen_world, "owner@acme.test", [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == (
        403,
        "MAKER_CHECKER_VIOLATION",
    )
    assert gen_world.listing(made.listing_id)["approved_version_id"] is None
