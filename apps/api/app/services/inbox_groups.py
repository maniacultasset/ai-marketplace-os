"""The grouped Inbox and the answer for many products (G1, prod bulk test 15 Nov 2026).

`list_groups(principal, view)` (`GET /inbox/groups`): the Inbox's open items, identical ones
across the brand's products in one group (`inbox_groups_model`). The rows are the Inbox lists' own
(`task_in_inbox`, `issue_in_inbox`, and for `all` only the issues no open question stands for), so
`total_items` and the sum of the group counts equal Home's `inbox.open_items` (`all`),
`inbox.questions` (`questions`) or `inbox.issues` (`issues`). The read re-checks the review queue
first, as the Inbox lists and Home do, then reads in one READ ONLY transaction under Home's 5 s
budget (`AnalyticsBudgetError` past it). Computed at read time: nothing is stored.

`answer_group(principal, key, path, body)` (`POST /inbox/groups/answer`, idempotent): one value
for up to 100 questions of one group, each task in its own SAVEPOINT, sorted by (product, task)
so every task locks its product, then itself, as a single answer does:

- a task this tenant does not have (missing, or another brand's: the same answer) is `gone`; a
  task whose key is not the group's is 422 at `task_ids[i]`; a group that is not `product_fact`
  or `exemption` is 422 at `group` (the brand manufacturer is set with `PATCH /brand-profile`);
- a task no longer open is `closed`; a product that already holds the fact is `has_value` and is
  never written (the never-overwrite rule);
- `product_fact`: `services.tasks.answer_in_uow`, checked against that task's own AnswerSpec
  (its category's allowed values): a refused value is `not_allowed`, a fact of another product
  level `wrong_level`; the fact, the task's `open -> applied`, and the audit rows
  `human_task.answered` and `human_task.applied` (actor: the caller) as for one answer;
- `exemption` (`value: true` only, D1): `product_edit.exempt_in_uow` (a barcode, or any status,
  is never touched), the task applied, the same two audit rows.

When nothing is applied and every skip is `not_allowed`, the whole answer is 422 at `value`. The
answer lists the affected listings that are not archived, each flagged `redraft` when it can be
written again now. Nothing is queued here: the dashboard redrafts each through
`POST /listings/{listing_id}/regenerate`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.analytics import _budget_error
from app.repositories.inbox_groups import ListingRef, ProductFactsRow
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services import inbox_groups_model as model
from app.services.product_facts import fact_stamp

View = Literal["all", "questions", "issues"]
VIEWS: Final[tuple[View, ...]] = ("all", "questions", "issues")
ANSWERABLE: Final = frozenset({"product_fact", "exemption"})


# ---- the read ----


def _fact_row(row: Any, variants_without_status: Mapping[uuid.UUID, int]) -> model.FactRow:
    is_style = row.parent_product_id is None and row.variation_theme is not None
    return model.FactRow(
        id=row.id,
        parent_id=row.parent_product_id,
        is_style=is_style,
        attributes=row.attributes or {},
        columns={
            "gender": row.gender,
            "brand_name": row.brand_name,
            "hsn_code": row.hsn_code,
            "country_of_origin": row.country_of_origin,
            "manufacturer_party_id": row.manufacturer_party_id,
            "identifier_status": row.identifier_status,
        },
        variants_without_status=variants_without_status.get(row.id, 0) if is_style else 0,
    )


def _listings_by_product(refs: Sequence[ListingRef]) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for ref in refs:
        out.setdefault(ref.product_id, []).append(ref.id)
    return out


async def _read(principal: Principal, view: View) -> dict[str, Any]:
    from app.services.issues import issue_fields
    from app.services.resolution.read import labels_for
    from app.services.tasks import task_fields

    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        reads = uow.inbox_groups
        await reads.tasks.bounded()
        tasks = await reads.tasks.open_rows() if view != "issues" else []
        issues = (
            await reads.issues.open_rows(without_question=view == "all")
            if view != "questions"
            else []
        )
        task_products = {t.task.product_id for t in tasks if t.task.product_id is not None}
        styles_of = {t.parent_product_id for t in tasks if t.parent_product_id is not None}
        facts = {r.id: r for r in await reads.products.facts(sorted(task_products | styles_of))}
        style_ids = [
            r.id
            for r in facts.values()
            if r.parent_product_id is None and r.variation_theme is not None
        ]
        without = await reads.products.variants_without_status(style_ids)
        listing_refs = await reads.listings.for_products(sorted(task_products | styles_of))
        brand_set = False
        if any(t.task.target_path == model.MANUFACTURER_TARGET for t in tasks):
            brand_set = await uow.brand_profiles.default_manufacturer() is not None
        members = _task_members(tasks, facts, without, listing_refs)
        members += _issue_members(issues)
        groups = model.build_groups(members)
        shown = groups[: model.GROUPS_MAX]
        task_rows = {t.task.id: t for t in tasks}
        issue_rows = {i.issue.id: i for i in issues}
        samples = [issue_rows[g.newest.item_id] for g in shown if g.item_kind == "issue"]
        labels = await labels_for(uow, [s.issue for s in samples])
    out_groups = []
    for g in shown:
        sample_task = None
        sample_issue = None
        if g.item_kind == "task":
            row = task_rows[g.newest.item_id]
            sample_task = {**task_fields(row.task), "blocking": row.blocking}
        else:
            found = issue_rows[g.newest.item_id]
            sample_issue = issue_fields(found.issue, labels.get(found.issue.id), found.product_id)
        out_groups.append(
            _group_out(g, brand_set and g.scope == "brand", sample_task, sample_issue)
        )
    return {
        "enabled": True,
        "as_of": datetime.now(UTC),
        "view": view,
        "total_items": len(members),
        "groups": out_groups,
        "more": len(groups) > model.GROUPS_MAX,
    }


def _task_members(
    tasks: Sequence[Any],
    facts: Mapping[uuid.UUID, ProductFactsRow],
    without: Mapping[uuid.UUID, int],
    refs: Sequence[ListingRef],
) -> list[model.Member]:
    listed = _listings_by_product(refs)
    out: list[model.Member] = []
    for t in tasks:
        task = t.task
        own = facts.get(task.product_id) if task.product_id is not None else None
        style = facts.get(t.parent_product_id) if t.parent_product_id is not None else None
        listing_ids: list[uuid.UUID] = []
        for owner in (task.product_id, t.parent_product_id):
            if owner is not None:
                listing_ids += listed.get(owner, [])
        out.append(
            model.Member(
                key=model.task_key(task.kind, task.target_path, task.expected_answer),
                item_kind="task",
                scope=model.task_scope(task.kind, task.target_path, task.expected_answer),
                at=task.created_at,
                blocking=bool(t.blocking),
                item_id=task.id,
                field=model.task_field(task.target_path),
                task_kind=task.kind,
                spec=task.expected_answer,
                product_id=task.product_id,
                sku=t.sku,
                title=t.title,
                category=t.category,
                listing_ids=tuple(listing_ids),
                has_value=model.has_value(
                    task.target_path,
                    None if own is None else _fact_row(own, without),
                    None if style is None else _fact_row(style, without),
                ),
            )
        )
    return out


def _issue_members(issues: Sequence[Any]) -> list[model.Member]:
    out: list[model.Member] = []
    for i in issues:
        issue = i.issue
        out.append(
            model.Member(
                key=model.issue_key(
                    issue.error_code, issue.field_path, issue.marketplace, issue.severity
                ),
                item_kind="issue",
                scope="per_item",
                at=issue.created_at,
                blocking=issue.severity == "error",
                item_id=issue.id,
                field=model.field_family(issue.field_path),
                error_code=issue.error_code,
                marketplace=issue.marketplace,
                product_id=i.product_id,
                sku=i.sku,
                title=i.title,
                category=i.category,
                listing_ids=(issue.listing_id,),
            )
        )
    return out


def _group_out(
    g: model.Group,
    brand_value_set: bool,
    sample_task: dict[str, Any] | None,
    sample_issue: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "key": g.key,
        "item_kind": g.item_kind,
        "scope": g.scope,
        "count": g.count,
        "products": g.products,
        "listings": g.listings,
        "blocking": g.blocking,
        "at": g.at,
        "field": g.field,
        "task_kind": g.task_kind,
        "error_code": g.error_code,
        "marketplace": g.marketplace,
        "answer": None if g.answer is None else dict(g.answer),
        "categories": g.categories,
        "brand_value_set": brand_value_set,
        "sample_task": sample_task,
        "sample_issue": sample_issue,
        "members": [
            {
                "task_id": m.item_id if m.item_kind == "task" else None,
                "issue_id": m.item_id if m.item_kind == "issue" else None,
                "product_id": m.product_id,
                "sku": m.sku,
                "title": m.title,
                "listing_ids": list(m.listing_ids[: model.MEMBER_LISTINGS_MAX]),
                "has_value": m.has_value,
                "at": m.at,
            }
            for m in g.members
        ],
        "members_more": g.members_more,
    }


def _disabled(view: View) -> dict[str, Any]:
    return {
        "enabled": False,
        "as_of": datetime.now(UTC),
        "view": view,
        "total_items": 0,
        "groups": [],
        "more": False,
    }


async def list_groups(principal: Principal, view: View) -> dict[str, Any]:
    if not get_settings().inbox_groups_enabled:
        return _disabled(view)
    from app.services.listing_display import recheck_queue

    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await recheck_queue(uow, principal)  # a stale "passed" opens its items first
    try:
        return await _read(principal, view)
    except DBAPIError as exc:
        budget = _budget_error(exc)
        if budget is None:
            raise
        raise budget from None


# ---- the answer ----


class _SkippedError(Exception):
    """Ends one task's savepoint (rolled back) with the reason it was skipped."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _orm_fact_row(row: Any) -> model.FactRow:
    """A locked product row as the never-overwrite rule reads it (no variant count: the
    exemption writer decides a style itself)."""
    return _fact_row(row, {})


async def _audit_applied(
    uow: TenantUnitOfWork, principal: Principal, task: Any, product_id: uuid.UUID, after: Any
) -> None:
    for action, body in (("human_task.answered", after), ("human_task.applied", None)):
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action=action,
            entity_type="product",
            entity_id=product_id,
            after=None if body is None else json.dumps(body, sort_keys=True),
            reason=task.target_path,
        )


async def _exempt(uow: TenantUnitOfWork, principal: Principal, task: Any, product: Any) -> None:
    from app.services.product_edit import exempt_in_uow
    from app.services.tasks import fact_edits

    fact = {
        "source": "seller",
        "captured_via": "human_task",
        "updated_at": fact_stamp(datetime.now(UTC)),
        "evidence_ref": f"human_task:{task.id}",
        "evidence_snapshot": {"question": task.question[:400], "answer": "GTIN exempt"},
    }
    if await exempt_in_uow(uow, principal, product, fact) == 0:
        raise _SkippedError("has_value")
    answer = {"value": True}
    if (
        await uow.human_tasks.answer_and_apply(task.id, answer=answer, user_id=principal.user_id)
        != 1
    ):
        raise _SkippedError("closed")
    await _audit_applied(uow, principal, task, product.id, {"provenance": {"identifier": fact}})
    await fact_edits(uow, principal, task.id, task.target_path)


async def _answer_one(
    uow: TenantUnitOfWork,
    principal: Principal,
    task_id: uuid.UUID,
    product_id: uuid.UUID,
    scope: str,
    value: Any,
) -> None:
    """One task in the caller's savepoint, or `_SkippedError` (the savepoint rolls back)."""
    from app.services.tasks import answer_in_uow

    product = await uow.products.get_for_update(product_id)
    if product is None:
        raise _SkippedError("gone")  # its product was deleted
    task = await uow.human_tasks.get_for_update(task_id)
    if task is None:
        raise _SkippedError("gone")
    if task.status != "open":
        raise _SkippedError("closed")
    if scope == "exemption":
        await _exempt(uow, principal, task, product)
        return
    style = None
    if product.parent_product_id is not None:
        style = await uow.products.get_by_id(product.parent_product_id)
    if model.has_value(
        task.target_path, _orm_fact_row(product), None if style is None else _orm_fact_row(style)
    ):
        raise _SkippedError("has_value")
    await answer_in_uow(uow, principal, task_id, {"value": value})


def _reason(exc: Exception) -> str | None:
    """The skip reason of a refusal inside one task's savepoint, or None to re-raise it."""
    if isinstance(exc, _SkippedError):
        return exc.reason
    if isinstance(exc, InputError):
        return "wrong_level" if exc.code is ErrorCode.FACT_LEVEL_INVALID else "not_allowed"
    if isinstance(exc, NotFoundError):
        return "gone"
    if isinstance(exc, ConflictError) and exc.code is ErrorCode.INVALID_STATUS_TRANSITION:
        return "closed"
    return None


async def _answer_group(
    uow: TenantUnitOfWork,
    principal: Principal,
    group: str,
    task_ids: Sequence[uuid.UUID],
    value: Any,
) -> dict[str, Any]:
    found = await uow.inbox_groups.tasks.by_ids(task_ids)
    skipped: list[dict[str, Any]] = []
    peeks: list[Any] = []
    for i, task_id in enumerate(task_ids):
        peek = found.get(task_id)
        if peek is None:
            skipped.append({"task_id": task_id, "product_id": None, "reason": "gone"})
            continue
        if model.task_key(peek.kind, peek.target_path, peek.expected_answer) != group:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"task_ids[{i}]")
        peeks.append(peek)
    scopes = {model.task_scope(p.kind, p.target_path, p.expected_answer) for p in peeks}
    if scopes - ANSWERABLE:
        raise InputError(ErrorCode.VALIDATION_FAILED, "group")
    if "exemption" in scopes and value is not True:
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    applied: list[dict[str, Any]] = []
    written: list[uuid.UUID] = []
    # plain values read before any savepoint: a rolled-back savepoint may expire loaded rows
    plan = sorted(
        (
            (p.product_id, p.id, model.task_scope(p.kind, p.target_path, p.expected_answer))
            for p in peeks
        ),
        key=lambda t: (str(t[0]), str(t[1])),
    )
    for product_id, task_id, scope in plan:
        if product_id is None:
            skipped.append({"task_id": task_id, "product_id": None, "reason": "gone"})
            continue
        try:
            async with uow.session.begin_nested():
                await _answer_one(uow, principal, task_id, product_id, scope, value)
        except (_SkippedError, InputError, NotFoundError, ConflictError) as exc:
            reason = _reason(exc)
            if reason is None:
                raise
            skipped.append({"task_id": task_id, "product_id": product_id, "reason": reason})
            continue
        applied.append({"task_id": task_id, "product_id": product_id})
        written.append(product_id)
    if not applied and skipped and all(s["reason"] == "not_allowed" for s in skipped):
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    return {
        "applied": applied,
        "skipped": skipped,
        "listings": await _affected(uow, written),
    }


async def _affected(
    uow: TenantUnitOfWork, product_ids: Sequence[uuid.UUID]
) -> list[dict[str, Any]]:
    """The listings (not archived) of the answered products and of their styles."""
    if not product_ids:
        return []
    rows = await uow.inbox_groups.products.facts(product_ids)
    owners = set(product_ids) | {r.parent_product_id for r in rows if r.parent_product_id}
    refs = await uow.inbox_groups.listings.for_products(sorted(owners))
    return [
        {
            "listing_id": ref.id,
            "product_id": ref.product_id,
            "marketplace": ref.marketplace,
            "status": ref.status,
            "redraft": model.redraft(ref.status, ref.paused),
        }
        for ref in refs
    ]


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


async def answer_group(principal: Principal, key: str, path: str, body: Any) -> dict[str, Any]:
    """The route's idempotent transaction: 200 with the outcome, stored under the key. The
    request hash covers the group, the value and the task ids in sorted order."""
    from app.services.idempotency import idempotent

    canonical = {
        "group": body.group,
        "task_ids": sorted(str(t) for t in body.task_ids),
        "value": body.value,
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        out = await _answer_group(uow, principal, body.group, list(body.task_ids), body.value)
        return 200, _json(out)

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored
