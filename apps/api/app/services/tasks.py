"""The human task inbox (S19 T21; DATABASE.md §5.7 :1338-1355). `evidence` (it may hold contact
data) and `answer` never leave the service.

SI3 `answer_task` (`POST /tasks/{task_id}/answer`, idempotent): the seller answers one fact
question (`missing_fact`, `conflicting_facts` on `facts.attributes.<key>`). A value is written
through the FactWriter as the seller's own fact (`captured_via 'human_task'`, `evidence_ref
'human_task:<id>'` with a bounded `evidence_snapshot`, DATABASE.md:1419); `skip` writes nothing.
The task then goes `open -> answered -> applied`. The same answer again is 200 with the current
row; any other answer to a closed task is 409 `INVALID_STATUS_TRANSITION`. Resuming the listing is
not automatic: the seller regenerates (SI3 out of scope).

SI9 `close_task` (`POST /tasks/{task_id}/close`, idempotent): the seller closes an account task
(`marketplace_error_review`, `permission_decision`) as `done` or `dismissed`, with an optional
note; the resolution is the task's answer (`open -> answered -> applied`) and `TaskOut.resolution`
shows it. One `human_task.closed` audit row on the account (the note is never audited).

Locks: the product row first, then the task (`FOR UPDATE`), the same order as a product PATCH
(which locks the product, then cancels its live questions), so the two never deadlock.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.uow import repositories_on, tenant_uow
from app.services.intake_values import MoneyError, parse_money
from app.services.photo_facts import ANSWER_MAX_PIECES, VOCAB, canonical, fold
from app.services.product_facts import STANDALONE, build_attributes, fact_stamp

FACT_KINDS: Final = frozenset({"missing_fact", "conflicting_facts"})
FACT_TARGET: Final = re.compile(r"^facts\.(?:attributes\.([a-z0-9_]{1,64})|(gender|brand_name))$")
# SI11 T17 (CP-D25): the answerable `products` columns (the identity facts Amazon needs)
GENDERS: Final = frozenset({"women", "men", "unisex", "girls", "boys", "baby"})
ADAPTER_SOURCES: Final = ("amazon_adapter", "flipkart_adapter")
SELLER_KINDS: Final = frozenset({"missing_fact", "conflicting_facts", "image_replacement"})
MAX_PIECES: Final = ANSWER_MAX_PIECES
# DATABASE.md:1419 / fact_map_valid: an evidence snapshot is at most 2048 bytes as JSON text
SNAPSHOT_BUDGET: Final = 1900

Keyset = tuple[datetime, uuid.UUID]
_FIELDS = (
    "id", "kind", "product_id", "colourway_id", "target_path", "marketplace_account_id",
    "image_id", "question", "reason", "expected_answer", "suggestions", "status",
    "answered_by_user_id", "answered_at", "resume_from", "applied_at", "resume_batch_id",
    "cancelled_reason", "created_at", "updated_at",
)  # fmt: skip


CLOSABLE: Final = frozenset({"marketplace_error_review", "permission_decision"})


def _resolution(row: Any) -> str | None:
    answer = getattr(row, "answer", None)
    if row.kind not in CLOSABLE or not isinstance(answer, dict):
        return None
    found = answer.get("resolution")
    return found if found in ("done", "dismissed") else None


def task_fields(row: Any) -> dict[str, Any]:
    return {**{name: getattr(row, name) for name in _FIELDS}, "resolution": _resolution(row)}


async def list_tasks(
    principal: Principal, status: str, *, after: Keyset | None, limit: int
) -> tuple[list[dict[str, Any]], bool, Keyset | None]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, more = await uow.human_tasks.page(status, after=after, limit=limit)
    next_key = (rows[-1].created_at, rows[-1].id) if more and rows else None
    return [task_fields(r) for r in rows], more, next_key


def _json(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if isinstance(value, uuid.UUID):
            out[name] = str(value)
        elif isinstance(value, datetime):
            out[name] = value.isoformat()
        else:
            out[name] = value
    return out


def _level(row: Any) -> str:
    if row.parent_product_id is not None:
        return "variant"
    if row.variation_theme is not None:
        return "style"
    return STANDALONE


def _snapshot(question: str, answer: object) -> dict[str, str]:
    """The question and the answer as text, the answer trimmed until the JSON fits the budget."""
    text = str(answer)[:500]
    while text:
        snapshot = {"question": question, "answer": text}
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) <= SNAPSHOT_BUDGET:
            return snapshot
        text = text[: max(0, len(text) - 25)]
    return {"question": question[:400], "answer": ""}


def _answer_value(key: str, value: Any, expected: Any) -> Any:
    """The value to store, or 422 at `value`, checked against the task's AnswerSpec
    (DATABASE.md:1411, :1422) whichever lane opened it:

    - `enum` takes one of `allowed_values` (the canonical form for a photo-vocabulary key,
      DATABASE.md:1417), `multi_enum` a non-empty list of them; without allowed values, nothing;
    - `number` an int or float (not a bool) within `min`..`max`;
    - `text` a string of at most `max_length` characters; `boolean` a bool;
    - `money_inr` a positive decimal string; `percent_composition` a string (the composition
      value type parses it in `build_attributes`);
    - every other kind (`file`, `acknowledge`, `reconnect`, an unknown or missing kind) no value.

    A piece count is always an int 1..50, whatever the spec says."""
    if key == "number_of_pieces":
        return _count(value, 1, MAX_PIECES)
    kind = expected.get("kind") if isinstance(expected, dict) else None
    if kind == "number":
        return _number(value, expected.get("min"), expected.get("max"))
    if kind in ("enum", "multi_enum"):
        allowed = expected.get("allowed_values")
        if not isinstance(allowed, list) or not allowed:
            raise InputError(ErrorCode.VALIDATION_FAILED, "value")
        if kind == "enum":
            return _member(key, value, allowed)
        if not isinstance(value, list) or not value:
            raise InputError(ErrorCode.VALIDATION_FAILED, "value")
        return [_member(key, item, allowed) for item in value]
    if kind == "text":
        limit = expected.get("max_length")
        if isinstance(value, str) and (not _real(limit) or len(value) <= limit):
            return value
    elif kind == "boolean":
        if isinstance(value, bool):
            return value
    elif kind == "money_inr":
        try:
            parse_money(value)
        except MoneyError:
            pass
        else:
            return value
    elif kind == "percent_composition" and isinstance(value, str):
        return value
    raise InputError(ErrorCode.VALIDATION_FAILED, "value")


def _real(bound: Any) -> bool:
    """An int or float bound (a bool is neither)."""
    return isinstance(bound, int | float) and not isinstance(bound, bool)


def _count(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    return value


def _number(value: Any, low: Any, high: Any) -> int | float:
    if not _real(value) or math.isnan(value) or math.isinf(value):
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    if (_real(low) and value < low) or (_real(high) and value > high):
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    return value  # type: ignore[no-any-return]


def _member(key: str, value: Any, allowed: list[Any]) -> Any:
    """The allowed entry `value` names: its canonical form when both are in the photo vocabulary,
    else the one entry with the same folded text."""
    if not isinstance(value, str):
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    if key in VOCAB:
        mapped = canonical(key, value)
        if mapped is not None and mapped in {canonical(key, entry) for entry in allowed}:
            return mapped
    folded = fold(value)
    hits = [entry for entry in allowed if isinstance(entry, str) and fold(entry) == folded]
    if not folded or len(hits) != 1:
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    return hits[0]


async def _answer(
    uow: Any, principal: Principal, task_id: uuid.UUID, answer: dict[str, Any]
) -> Any:
    peek = await uow.human_tasks.get_by_id(task_id)
    if peek is None:
        raise NotFoundError("task_id")
    if peek.kind not in FACT_KINDS or peek.product_id is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "task_id")
    found = FACT_TARGET.match(peek.target_path or "")
    if found is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "task_id")
    product = await uow.products.get_for_update(peek.product_id)
    if product is None:
        raise NotFoundError("task_id")
    task = await uow.human_tasks.get_for_update(task_id)
    if task is None:
        raise NotFoundError("task_id")
    if task.status != "open":
        if task.status in ("answered", "applied") and task.answer == answer:
            return task
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "human_tasks")
    key = found.group(1)
    column = found.group(2)
    written: dict[str, Any] = {}
    if column is not None:
        if "value" in answer:
            value = await _column_value(uow, column, answer["value"])
            column_fact: dict[str, Any] = {
                "source": "seller",
                "captured_via": "human_task",
                "updated_at": fact_stamp(datetime.now(UTC)),
                "evidence_ref": f"human_task:{task.id}",
                "evidence_snapshot": _snapshot(task.question, answer["value"]),
            }
            await uow.facts.set_typed(product.id, column, value, column_fact)
            # the column UPDATE bypasses the ORM: the product row this transaction loaded is
            # stale, and the revalidation below must read the answered value
            uow.session.expire(product, [column, "provenance"])
            written = {column: value}
    elif "value" in answer:
        value = _answer_value(key, answer["value"], task.expected_answer)
        defs = await uow.attribute_definitions.resolve([key])
        try:
            built = build_attributes(
                {key: value}, defs, now=datetime.now(UTC), level=_level(product)
            )
        except InputError as exc:
            raise InputError(exc.code, "value") from None
        fact = {
            **built[key],
            "captured_via": "human_task",
            "evidence_ref": f"human_task:{task.id}",
            "evidence_snapshot": _snapshot(task.question, answer["value"]),
        }
        await uow.facts.set_attribute(product.id, key, fact)
        written = {key: fact}
    if (
        await uow.human_tasks.answer_and_apply(task.id, answer=answer, user_id=principal.user_id)
        != 1
    ):
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "human_tasks")
    for action, after in (
        ("human_task.answered", {"attributes": written} if written else {}),
        ("human_task.applied", None),
    ):
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action=action,
            entity_type="product",
            entity_id=product.id,
            after=None if after is None else json.dumps(after, sort_keys=True),
            reason=task.target_path,
        )
    if written and task.target_path:
        await _fact_edits(uow, principal, task.id, task.target_path)
    return await uow.human_tasks.get_for_update(task.id)


async def _column_value(uow: Any, column: str, raw: Any) -> str:
    """SI11 T17: a `facts.gender` or `facts.brand_name` answer, checked as the product PATCH
    checks it (the gender vocabulary; a brand only from the tenant's own brand names)."""
    from app.services.products import checked_brand

    if not isinstance(raw, str) or not raw.strip() or len(raw) > 200:
        raise InputError(ErrorCode.VALIDATION_FAILED, "value")
    if column == "gender":
        value = raw.strip().casefold()
        if value not in GENDERS:
            raise InputError(ErrorCode.VALUE_NOT_ALLOWED, "value")
        return value
    try:
        brand = await checked_brand(uow, raw.strip())
    except InputError as exc:
        raise InputError(exc.code, "value") from None
    assert brand is not None  # noqa: S101 - a non-blank value matched or raised
    return brand


async def _fact_edits(uow: Any, principal: Principal, task_id: uuid.UUID, path: str) -> None:
    """SI11 T17 (P:B6, T51): an answer behind a marketplace refusal makes a fact-only version of
    each listing whose refusal the task answers, once no other live question stands on it; the
    answerer is its editor, so the answerer never approves it (`lv_guard`)."""
    from app.services.listing_edit import fact_edit

    for listing_id in await uow.listing_issues.listings_for_task(task_id, ADAPTER_SOURCES):
        live = await uow.resolution.task_kinds(await uow.listing_issues.live_task_ids(listing_id))
        if any(kind in SELLER_KINDS for kind, _status in live.values()):
            continue  # another question still open: one version once every answer is in
        listing = await uow.listings.get_for_update(listing_id)
        if listing is None:
            continue
        paths = await uow.listing_issues.answered_paths(listing_id, ADAPTER_SOURCES, SELLER_KINDS)
        await fact_edit(uow, principal, listing, paths or [path])


async def answer_task(
    principal: Principal, key: str, path: str, task_id: uuid.UUID, body: Any
) -> dict[str, Any]:
    """The route's idempotent transaction: 200 with the task row, stored under the key."""
    from app.services.idempotency import idempotent

    answer: dict[str, Any] = {"skip": True} if body.skip else {"value": body.value}
    payload = json.dumps(answer, sort_keys=True, ensure_ascii=False).encode()

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        row = await _answer(uow, principal, task_id, answer)
        return 200, _json(task_fields(row))

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored


async def _close(uow: Any, principal: Principal, task_id: uuid.UUID, answer: dict[str, Any]) -> Any:
    """SI9 T16: `open -> answered -> applied` with the resolution as the answer. The same close
    again is 200 with the current row; any other close of a closed task is 409."""
    task = await uow.human_tasks.get_for_update(task_id)
    if task is None:
        raise NotFoundError("task_id")
    if task.kind not in CLOSABLE:
        raise InputError(ErrorCode.VALIDATION_FAILED, "task_id")
    if task.status != "open":
        if task.status in ("answered", "applied") and task.answer == answer:
            return task
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "human_tasks")
    if (
        await uow.human_tasks.answer_and_apply(task.id, answer=answer, user_id=principal.user_id)
        != 1
    ):
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "human_tasks")
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="human_task.closed",
        entity_type="marketplace_account",
        entity_id=task.marketplace_account_id,
        # the account audit keeps `status`/`settings` only: the resolution rides in `reason`
        after=json.dumps({"status": "applied"}),
        reason=f"{task.target_path} {answer['resolution']}",
    )
    return await uow.human_tasks.get_for_update(task.id)


async def close_task(
    principal: Principal, key: str, path: str, task_id: uuid.UUID, body: Any
) -> dict[str, Any]:
    """`POST /tasks/{task_id}/close` (SI9 T16): the route's idempotent transaction, stored under
    the key. Tenant-scoped through `tenant_uow` (another tenant's task reads as missing)."""
    from app.services.idempotency import idempotent

    answer: dict[str, Any] = {"resolution": body.resolution}
    if body.note is not None:
        answer["note"] = body.note
    payload = json.dumps(answer, sort_keys=True, ensure_ascii=False).encode()

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        row = await _close(uow, principal, task_id, answer)
        return 200, _json(task_fields(row))

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored
