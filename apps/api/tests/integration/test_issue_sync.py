"""S19 T18 (AC 13; CP-D2; DATABASE.md:1330-1334): issues come from version-linked runs, in the
same transaction as the run, and close with the resolution of the run's cause."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating
from .products_support import bearer_for
from .review_support import OPERATOR, REVIEWER, Made, generated, new_product, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
CLEAN: dict[str, Any] = {
    "title": "Cotton Kurta Set for Women",
    "bullets": ["Soft cotton fabric"],
    "description": "A cotton kurta set with a straight fit.",
}


def _issues(world: GenWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = world.conn.execute(
        "SELECT * FROM listing_issues WHERE listing_id = %s ORDER BY created_at, id", [listing_id]
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


async def _blocked(world: GenWorld) -> Made:
    """A `stub:blocked` generation: the backend re-check finds the over-long title."""
    made = await generated(world, scenario="stub:blocked")
    assert world.listing(made.listing_id)["status"] == "blocked"
    return made


async def test_issue_sync_from_generation_recheck(gen_world: GenWorld) -> None:
    made = await _blocked(gen_world)
    rows = _issues(gen_world, made.listing_id)
    assert rows, "the re-check's blocking item opens an issue"
    issue = rows[0]
    assert issue["status"] == "open" and issue["severity"] == "error"
    assert issue["listing_version_id"] == made.version_id
    assert issue["job_id"] == made.job_id
    run = gen_world.scalar(
        "SELECT id FROM validation_runs WHERE listing_version_id = %s", [made.version_id]
    )
    assert issue["validation_run_id"] == run and issue["last_validation_run_id"] == run
    assert issue["dedupe_key"].startswith(f"rules_engine|{issue['field_path']}|")


async def test_issue_sync_attempt_rows_not_synced(gen_world: GenWorld) -> None:
    """Three in-graph attempts wrote runs with no version; only the linked re-check counts."""
    made = await _blocked(gen_world)
    attempts = gen_world.count(
        "validation_runs", "listing_id = %s AND listing_version_id IS NULL", [made.listing_id]
    )
    assert attempts >= 1
    assert all(r["occurrences"] == 1 for r in _issues(gen_world, made.listing_id))


async def _patch(api: httpx.AsyncClient, world: GenWorld, made: Made, title: str) -> Any:
    headers = {**bearer_for(world, OPERATOR), "If-Match": f'"{made.version_id}"'}
    return await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={"content": {**CLEAN, "title": title}},
    )


async def test_issue_sync_closes_missing_human_edit(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await _blocked(gen_world)
    response = await _patch(api, gen_world, made, CLEAN["title"])
    assert response.status_code == 200, response.text
    rows = _issues(gen_world, made.listing_id)
    assert rows and all((r["status"], r["resolution"]) == ("resolved", "human_edit") for r in rows)


async def test_issue_sync_dedupes_and_sets_recurrence(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await _blocked(gen_world)
    [first] = [r for r in _issues(gen_world, made.listing_id) if r["status"] == "open"][:1]
    fixed = await _patch(api, gen_world, made, CLEAN["title"])
    fixed_made = Made(made.listing_id, uuid.UUID(fixed.json()["current_version_id"]), made.job_id)
    long_title = "Long Silk Kurta Set " * 20
    again = await _patch(api, gen_world, fixed_made, long_title.strip())
    assert again.status_code == 200 and again.json()["status"] == "blocked"
    reopened = [
        r for r in _issues(gen_world, made.listing_id)
        if r["status"] == "open" and r["dedupe_key"] == first["dedupe_key"]
    ]  # fmt: skip
    assert len(reopened) == 1 and reopened[0]["recurrence_of_id"] == first["id"]
    again_made = Made(made.listing_id, uuid.UUID(again.json()["current_version_id"]), made.job_id)
    third = await _patch(api, gen_world, again_made, long_title.strip() + " x")
    assert third.status_code == 200
    [row] = [r for r in _issues(gen_world, made.listing_id) if r["id"] == reopened[0]["id"]]
    assert row["occurrences"] == 2 and row["status"] == "open"


async def test_issue_sync_closes_missing_regenerated(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await _blocked(gen_world)
    headers = {**bearer_for(gen_world, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/listings/{made.listing_id}/regenerate", headers=headers)
    assert response.status_code == 202
    await run_job(uuid.UUID(response.json()["job_id"]))
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    rows = _issues(gen_world, made.listing_id)
    assert rows and all(r["resolution"] == "regenerated" for r in rows)


async def test_issue_sync_closes_missing_revalidated_clean(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A synthetic open issue on a clean listing closes on `validate` (a rule revision forces a
    fresh run with new pins)."""
    from app.repositories.listing_issues import NewIssue
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    principal = gen_world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await uow.listing_issues.upsert_open(
            NewIssue(
                listing_id=made.listing_id,
                marketplace="amazon",
                source="rules_engine",
                field_path="content.title",
                error_code="VALUE_NOT_ALLOWED",
                error_class="invalid_value",
                retry_class="user_action",
                severity="warning",
                message="old warning",
                dedupe_key="rules_engine|content.title|VALUE_NOT_ALLOWED|",
            )
        )
    gen_world.conn.execute(
        "UPDATE rules SET revision = nextval('rules_revision_seq') WHERE id = (SELECT id FROM "
        "rules WHERE marketplace = 'amazon' AND tenant_id IS NULL ORDER BY id LIMIT 1)"
    )
    response = await api.post(
        "/api/v1/listings/validate",
        headers=bearer_for(gen_world, OPERATOR),
        json={"listing_id": str(made.listing_id)},
    )
    assert response.status_code == 200
    [row] = _issues(gen_world, made.listing_id)
    assert (row["status"], row["resolution"]) == ("resolved", "revalidated_clean")


async def test_issue_sync_same_transaction(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault in the sync rolls the edit back: no version, no run, no issue change."""
    from app.repositories.listing_issues import ListingIssueRepository

    made = await _blocked(gen_world)
    before = (
        gen_world.count("validation_runs", "listing_id = %s", [made.listing_id]),
        gen_world.count("listing_versions", "listing_id = %s", [made.listing_id]),
        [r["status"] for r in _issues(gen_world, made.listing_id)],
    )

    async def broken(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("injected sync fault")

    monkeypatch.setattr(ListingIssueRepository, "close_missing", broken)
    response = await _patch(api, gen_world, made, CLEAN["title"])
    assert response.status_code == 500
    after = (
        gen_world.count("validation_runs", "listing_id = %s", [made.listing_id]),
        gen_world.count("listing_versions", "listing_id = %s", [made.listing_id]),
        [r["status"] for r in _issues(gen_world, made.listing_id)],
    )
    assert after == before


def _adapter_report(*fields: str) -> ErrorReport:
    items = [
        ErrorItem(
            field=f,
            error_code=ErrorCode.VALUE_NOT_ALLOWED,
            severity=Severity.ERROR,
            message="The marketplace refused the value.",
            source=ErrorSource.AMAZON_ADAPTER,
            retryable=False,
            raw_code="90220",
        )
        for f in fields
    ]
    return ErrorReport(status="failed" if items else "passed", errors=items)


async def _adapter_sync(world: GenWorld, made: Made, report: ErrorReport, **kw: Any) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.issues import sync_issues

    async with tenant_uow(world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        return await sync_issues(
            uow, listing, "amazon_adapter", report, run_id=None, cause="publish", **kw
        )


async def test_sync_issues_adapter_source_no_run_id(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    result = await _adapter_sync(gen_world, made, _adapter_report("content.title"))
    assert result.opened == 1
    [row] = _issues(gen_world, made.listing_id)
    assert row["validation_run_id"] is None and row["source"] == "amazon_adapter"
    assert row["marketplace_code"] == "90220"
    await _adapter_sync(gen_world, made, _adapter_report())
    await _adapter_sync(gen_world, made, _adapter_report("content.title"))
    rows = _issues(gen_world, made.listing_id)
    assert rows[-1]["recurrence_of_id"] == row["id"]


async def test_issue_sync_scope_fields_partial_publish(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title", "content.bullets"))
    result = await _adapter_sync(gen_world, made, _adapter_report(), scope_fields=["content.title"])
    assert result.closed == 1
    status = {r["field_path"]: r["status"] for r in _issues(gen_world, made.listing_id)}
    assert status == {"content.title": "resolved", "content.bullets": "open"}


async def test_issue_sync_scope_none_is_full_scope(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title", "content.bullets"))
    result = await _adapter_sync(gen_world, made, _adapter_report())
    assert result.closed == 2


async def test_issue_sync_failed_generation_writes_none(gen_world: GenWorld) -> None:
    """A failed generation saves no version, so nothing reaches the inbox."""
    accepted = await make_generating(
        gen_world, sku=await new_product(gen_world), instructions="stub:failed"
    )
    await run_job(accepted.job_id)
    assert _issues(gen_world, accepted.listing_id) == []
