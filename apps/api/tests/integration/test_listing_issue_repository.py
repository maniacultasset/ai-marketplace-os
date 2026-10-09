"""S19 T17 (DW6; AC 13; DATABASE.md:1304-1336): `ListingIssueRepository`, as `app_rw`."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from app.core.errors import ConflictError

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import REVIEWER, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def new_issue(made: Made, **over: Any) -> Any:
    from app.repositories.listing_issues import NewIssue

    field = over.pop("field_path", "content.title")
    code = over.pop("error_code", "VALUE_NOT_ALLOWED")
    source = over.pop("source", "rules_engine")
    defaults: dict[str, Any] = {
        "listing_id": made.listing_id,
        "marketplace": "amazon",
        "source": source,
        "field_path": field,
        "error_code": code,
        "error_class": "content_error",
        "retry_class": "user_action",
        "severity": "warning",
        "message": "The title uses a word the brand avoids.",
        "dedupe_key": f"{source}|{field}|{code}|",
        "listing_version_id": made.version_id,
    }
    return NewIssue(**{**defaults, **over})


async def _uow(world: GenWorld, fn: Any, email: str = REVIEWER) -> Any:
    from app.repositories.uow import tenant_uow

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await fn(uow.listing_issues)


async def _upsert(world: GenWorld, issue: Any) -> tuple[Any, bool]:
    async def go(repo: Any) -> tuple[Any, bool]:
        row, created = await repo.upsert_open(issue)
        return (row.id, row.occurrences, row.recurrence_of_id), created

    return await _uow(world, go)  # type: ignore[no-any-return]


async def test_upsert_new_then_dedupe_occurrences(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    (first, n1, _), created1 = await _upsert(gen_world, new_issue(made))
    (second, n2, _), created2 = await _upsert(gen_world, new_issue(made, message="Again."))
    assert created1 is True and created2 is False
    assert first == second and (n1, n2) == (1, 2)
    assert gen_world.scalar("SELECT message FROM listing_issues WHERE id = %s", [first]) == "Again."


async def _close(world: GenWorld, made: Made, source: str, keep: list[str], **kw: Any) -> int:
    async def go(repo: Any) -> int:
        return await repo.close_missing(  # type: ignore[no-any-return]
            made.listing_id, source, keep, kw.pop("resolution", "revalidated_clean"), **kw
        )

    return await _uow(world, go)  # type: ignore[no-any-return]


async def test_recurrence_links_latest_closed(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    (first, _, _), _ = await _upsert(gen_world, new_issue(made))
    assert await _close(gen_world, made, "rules_engine", []) == 1
    (again, _, recurrence), created = await _upsert(gen_world, new_issue(made))
    assert created and again != first and recurrence == first


async def test_close_missing_only_this_source(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _upsert(gen_world, new_issue(made))
    await _upsert(gen_world, new_issue(made, source="schema"))
    assert await _close(gen_world, made, "schema", []) == 1
    statuses: dict[Any, Any] = dict(
        gen_world.conn.execute(
            "SELECT source, status FROM listing_issues WHERE listing_id = %s", [made.listing_id]
        ).fetchall()
    )
    assert statuses == {"schema": "resolved", "rules_engine": "open"}


async def test_close_missing_keeps_present_keys(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _upsert(gen_world, new_issue(made))
    other = new_issue(made, field_path="content.description")
    await _upsert(gen_world, other)
    assert await _close(gen_world, made, "rules_engine", [other.dedupe_key]) == 1


async def test_close_missing_scope_fields_leaves_unsent_open(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    title = new_issue(made, source="amazon_adapter", field_path="content.title")
    bullets = new_issue(made, source="amazon_adapter", field_path="content.bullets")
    await _upsert(gen_world, title)
    await _upsert(gen_world, bullets)
    closed = await _close(
        gen_world, made, "amazon_adapter", [], resolution="marketplace_cleared",
        field_paths=["content.title"],
    )  # fmt: skip
    assert closed == 1
    open_paths = {
        r[0]
        for r in gen_world.conn.execute(
            "SELECT field_path FROM listing_issues WHERE listing_id = %s AND status = 'open'",
            [made.listing_id],
        ).fetchall()
    }
    assert open_paths == {"content.bullets"}


async def test_close_missing_empty_scope_closes_none(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _upsert(gen_world, new_issue(made, source="amazon_adapter"))
    assert await _close(gen_world, made, "amazon_adapter", [], field_paths=[]) == 0


async def _page(world: GenWorld, filters: Any, after: Any = None, limit: int = 50) -> Any:
    async def go(repo: Any) -> Any:
        rows, more = await repo.page(filters, after=after, limit=limit)
        return [(r.created_at, r.id) for r in rows], more

    return await _uow(world, go)


async def test_page_open_default_newest_first(gen_world: GenWorld) -> None:
    from app.repositories.listing_issues import IssueFilter

    made = await generated(gen_world)
    ids = []
    for path in ("content.title", "content.description", "content.bullets"):
        (issue_id, _, _), _ = await _upsert(gen_world, new_issue(made, field_path=path))
        ids.append(issue_id)
    await _close(
        gen_world,
        made,
        "rules_engine",
        [new_issue(made).dedupe_key, new_issue(made, field_path="content.bullets").dedupe_key],
    )
    rows, more = await _page(gen_world, IssueFilter(listing_id=made.listing_id))
    assert [i for _, i in rows] == [ids[2], ids[0]] and more is False
    rows, _ = await _page(gen_world, IssueFilter(status="resolved", listing_id=made.listing_id))
    assert [i for _, i in rows] == [ids[1]]


@pytest.mark.parametrize("field", ["source", "error_code", "marketplace"])
async def test_page_filters_each(gen_world: GenWorld, field: str) -> None:
    from app.repositories.listing_issues import IssueFilter

    made = await generated(gen_world)
    await _upsert(gen_world, new_issue(made))
    await _upsert(gen_world, new_issue(made, source="schema", error_code="FIELD_TOO_LONG"))
    wanted = {"source": "schema", "error_code": "FIELD_TOO_LONG", "marketplace": "flipkart"}
    rows, _ = await _page(
        gen_world, IssueFilter(listing_id=made.listing_id, **{field: wanted[field]})
    )
    assert len(rows) == (0 if field == "marketplace" else 1)


async def test_page_two_pages(gen_world: GenWorld) -> None:
    from app.repositories.listing_issues import IssueFilter

    made = await generated(gen_world)
    for n in range(3):
        await _upsert(gen_world, new_issue(made, field_path=f"attributes.attr_{n}"))
    first, more = await _page(gen_world, IssueFilter(listing_id=made.listing_id), limit=2)
    second, more2 = await _page(gen_world, IssueFilter(listing_id=made.listing_id), first[-1], 2)
    assert more and not more2
    assert len({i for _, i in first + second}) == 3


async def test_dismiss_cas_error_zero_rows(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    (error_id, _, _), _ = await _upsert(gen_world, new_issue(made, severity="error"))

    async def go(repo: Any) -> Any:
        return await repo.dismiss(error_id, gen_world.user(REVIEWER))

    assert await _uow(gen_world, go) is None
    assert gen_world.scalar("SELECT status FROM listing_issues WHERE id = %s", [error_id]) == "open"


def _raw(world: GenWorld, sql: str, params: list[Any]) -> str:
    with pytest.raises(psycopg.Error) as caught, world.conn.transaction():
        world.conn.execute(sql, params)
    return str(caught.value.sqlstate)


async def test_raw_dismiss_error_grd01(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    (error_id, _, _), _ = await _upsert(gen_world, new_issue(made, severity="error"))
    state = _raw(
        gen_world,
        "UPDATE listing_issues SET status = 'dismissed', resolution = 'accepted_warning', "
        "resolved_at = now(), resolved_by_user_id = %s WHERE id = %s",
        [gen_world.user(REVIEWER), error_id],
    )
    assert state in ("GRD01", "23514")  # the guard, or the dismissed CHECK (severity='warning')


async def test_raw_reopen_closed_grd01(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    (issue_id, _, _), _ = await _upsert(gen_world, new_issue(made))
    await _close(gen_world, made, "rules_engine", [])
    state = _raw(
        gen_world,
        "UPDATE listing_issues SET status = 'open', resolution = NULL, resolved_at = NULL "
        "WHERE id = %s",
        [issue_id],
    )
    assert state == "GRD01"


async def test_upsert_other_tenant_listing_refused(gen_world: GenWorld) -> None:
    """A bharat listing id in an acme write fails the tenant FK (never a cross-tenant row)."""
    bharat = await generated(gen_world, "operator@bharat.test")
    with pytest.raises(ConflictError):
        await _upsert(gen_world, new_issue(bharat))
