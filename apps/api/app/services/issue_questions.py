"""Issue-sync step 4: one Inbox question per seller-fixable fact or image (SI4 A13, A14;
DATABASE.md:1390-1394 step 4; spec L1015-1037 "ask seller", "one clear question").

`open_questions(uow, listing, *, critical_keys, images)` runs after a check point's issue syncs
(the generation persist, a content edit, a validate of the current version), for our own checks
only, sources `rules_engine`, `vision_model` and SI9's `schema` (the marketplace check before
approval; CP-D5: real marketplace answers, `<marketplace>_adapter`, are phase B):

- an open **error** row whose route asks `missing_fact` (an `ask` row, or a `deterministic` row
  with that fallback whose fix did not apply, CP-D15) on a dictionary key → one `missing_fact`
  task per fact at `facts.attributes.<key>` (SI3's shape, D19(a); a column-stored key uses
  `facts.<key>`), shared by every listing of the product (`ht_one_live_fact`), and the row
  linked to it;
- an open error row on `images[<i>]` (A14) → one `image_replacement` task per image;
- a row with no dictionary target stays an open issue with its suggestion.

The shared blocking rule (Approval record, 2026-11-07): a question never blocks a listing by
itself. Only an open blocking issue behind it does, so a question opens only behind an error row,
and a task no issue links to (another lane's photo question) never moves a listing.

`reconcile_tasks(uow, listing)` cancels each live task of the listing whose linked issues are all
closed (`issues_closed`); a task with no linked issue is never cancelled here (D19(c)).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction
from mkos_schemas.errors import ErrorItem
from mkos_schemas.jobs import CategoryTemplate

from app.core.logging import get_logger
from app.repositories.attribute_definitions import AttributeDef
from app.repositories.uow import TenantUnitOfWork
from app.services.auto_fix.policy import IMAGE_REPLACEMENT, MISSING_FACT, ask_kind, fact_key, route
from app.services.auto_fix.text import (
    answer_choices,
    expected_answer,
    image_question,
    question_for,
)
from app.services.generation_result import Move

log = get_logger(__name__)

QUESTION_SOURCES: Final = ("rules_engine", "vision_model", "schema")
QUESTION_MOVE: Final = Move("blocked", "human_task_opened", "human_task_open")
FACT_ROOT: Final = "facts"
_IMAGE_PREFIX: Final = "images["


def _item(row: Any) -> ErrorItem:
    """The stored row as the item the policy routes (its classification, never re-derived)."""
    return ErrorItem(
        field=row.field_path,
        error_code=ErrorCode(row.error_code),
        message=row.message,
        source=ErrorSource(row.source),
        retryable=False,
        error_class=ErrorClass(row.error_class),
        auto_fixable=row.fix_action is not None,
        fix_action=None if row.fix_action is None else FixAction(row.fix_action),
    )


def fact_target(definition: AttributeDef) -> str:
    """`facts.attributes.<key>` for an attribute-stored key (SI3's shape), else `facts.<key>`."""
    if definition.storage == "attributes":
        return f"{FACT_ROOT}.attributes.{definition.key}"
    return f"{FACT_ROOT}.{definition.key}"


def _image_index(field: str) -> int | None:
    if not (field.startswith(_IMAGE_PREFIX) and field.endswith("]")):
        return None
    raw = field[len(_IMAGE_PREFIX) : -1]
    return int(raw) if raw.isdigit() else None


async def _refused_values(uow: Any, owner: uuid.UUID, key: str, group: list[Any]) -> list[str]:
    """QA 2026-11-09: the values a marketplace just refused for `key` (the row's sent value,
    else the product's current fact), so the question never offers them back."""
    refusals = [
        r
        for r in group
        if str(r.source).endswith("_adapter") and r.error_code == ErrorCode.VALUE_NOT_ALLOWED.value
    ]
    if not refusals:
        return []
    out = [str(r.value_submitted) for r in refusals if getattr(r, "value_submitted", None)]
    product = await uow.products.get_by_id(owner)
    fact = (getattr(product, "attributes", None) or {}).get(key) if product is not None else None
    if isinstance(fact, dict) and isinstance(fact.get("value"), str):
        out.append(fact["value"])
    return out


async def open_questions(
    uow: TenantUnitOfWork,
    listing: Any,
    *,
    critical_keys: frozenset[str],
    template: CategoryTemplate | None,
    images: Sequence[Any] = (),
    rows: Sequence[Any] | None = None,
) -> list[uuid.UUID]:
    """Open (or reuse) the questions behind this listing's open error rows and link the rows;
    returns the task ids. `images` are the version's `ImageRef`s (`images[<i>]` → image id).

    SI11 T17: `rows` are the marketplace's rows the resolution planner gave the `seller` lane
    (never every adapter row, so a staff or marketplace row never asks the seller); a fact path
    then asks `missing_fact` and an image path `image_replacement` when the fix policy names no
    question.

    SI13 T9 (CP-D11): a row on a child item (`listing_item_id`) about a variation axis
    (`facts.attributes.size|color`) asks the child product, the owner of that value; every
    other row asks the listing's product (the style or the standalone)."""
    chosen = rows is not None
    if rows is None:
        rows = await uow.listing_issues.open_errors(listing.id, QUESTION_SOURCES)
    facts: dict[tuple[uuid.UUID, str], list[Any]] = {}
    pictures: dict[uuid.UUID, list[Any]] = {}
    children = await _child_items(uow, listing, rows)
    for row in rows:
        kind = ask_kind(route(_item(row), source=row.source, critical_keys=critical_keys))
        if kind is None and chosen:
            kind = IMAGE_REPLACEMENT if _image_index(row.field_path) is not None else MISSING_FACT
        if kind == MISSING_FACT:
            key = fact_key(row.field_path)
            if key is not None:
                owner = listing.product_id
                child = children.get(getattr(row, "listing_item_id", None))
                if child is not None and key in AXIS_KEYS:
                    owner = child
                facts.setdefault((owner, key), []).append(row)
        elif kind == IMAGE_REPLACEMENT:
            index = _image_index(row.field_path)
            if index is not None and index < len(images):
                pictures.setdefault(images[index].image_id, []).append(row)
    opened: list[uuid.UUID] = []
    if facts:
        keys = sorted({key for _owner, key in facts})
        known = {d.key: d for d in await uow.attribute_definitions.resolve(keys)}
        for (owner, key), group in sorted(facts.items(), key=lambda kv: (kv[0][1], str(kv[0][0]))):
            definition = known.get(key)
            if definition is None or definition.key != key:
                continue  # no dictionary target: the row stays an open issue (P:§2.6)
            # SI9 CP-D23: a template enum key offers the template's accepted values only
            refused = await _refused_values(uow, owner, key, group)
            choices = answer_choices(definition.vocabulary, template, key, exclude=refused)
            value_type = definition.value_type
            if value_type == "text" and choices is not None and not definition.vocabulary:
                value_type = "enum"
            question, reason = question_for(
                ErrorCode(group[0].error_code), key, "", value_type=value_type
            )
            task = await uow.human_tasks.open_fact_task(
                owner,
                fact_target(definition),
                question,
                reason,
                expected_answer(value_type, vocabulary=choices, unit=definition.unit),
            )
            await uow.listing_issues.link_ids([r.id for r in group], task)
            opened.append(task)
    for image_id, group in pictures.items():
        slot = next((i.slot for i in images if i.image_id == image_id), "product")
        checks = sorted({r.marketplace_code or r.error_code for r in group})
        question, reason = image_question(str(getattr(slot, "value", slot)), checks)
        task = await uow.human_tasks.open_image_task(listing.product_id, image_id, question, reason)
        await uow.listing_issues.link_ids([r.id for r in group], task)
        opened.append(task)
    if opened:
        log.info("issues.questions_opened", listing_id=str(listing.id), tasks=len(opened))
    return opened


AXIS_KEYS: Final = frozenset({"size", "color"})


async def _child_items(
    uow: TenantUnitOfWork, listing: Any, rows: Sequence[Any]
) -> dict[uuid.UUID | None, uuid.UUID]:
    """listing item id -> its product, for the child items the rows name (SI13 T9)."""
    if not any(getattr(r, "listing_item_id", None) for r in rows):
        return {}
    items = await uow.listing_items.for_listing(listing.id)
    return {i.id: i.product_id for i in items if i.item_role == "child"}


async def reconcile_tasks(uow: TenantUnitOfWork, listing: Any) -> int:
    """Cancel each live task of the listing whose linked issues are all closed.

    The product's image lock comes first (round 3 F3): `cancel_if_closed` keeps each task row
    it locks until commit, and `open_image_task` takes this lock before its row, so every task
    row lock of a product's step 4 is taken under the one lock. Two listings of one product
    (or crossed fact questions) then queue instead of deadlocking. The lock is per transaction
    and re-entrant, so the later take inside `open_image_task` is a no-op.

    The product row lock comes before it: SI3's photo step (`apply_photo_decision`) holds that
    row while it inserts photo questions on the same `ht_one_live_fact` keys, so a step 4 that
    took the image lock first could wait on such an uncommitted question while the photo
    persist waits on the image lock. Every path then takes product row, image lock, task rows."""
    await uow.products.get_for_update(listing.product_id)
    await uow.images.lock_product_images(listing.product_id)
    cancelled = 0
    for task_id in await uow.listing_issues.live_task_ids(listing.id):
        if await uow.human_tasks.cancel_if_closed(task_id):
            cancelled += 1
    if cancelled:
        log.info("issues.questions_cancelled", listing_id=str(listing.id), tasks=cancelled)
    return cancelled


QUESTION_FROM: Final = frozenset({"validated", "needs_review"})


def can_question_move(status: str, reason: str | None) -> bool:
    """The seeded `human_task_opened` edges a human check point can take from the status it
    just reached: `validated`/`needs_review` → blocked, and the reason-only `blocked` →
    `blocked` from `validation_errors` (01_transitions.up.sql:49-54)."""
    return status in QUESTION_FROM or (status == "blocked" and reason == "validation_errors")


async def ask_after_check(
    uow: TenantUnitOfWork,
    listing: Any,
    actor: Any,
    images: Sequence[Any],
    *,
    template: CategoryTemplate | None,
) -> Any:
    """A human check point's step 4 (edit, validate): reconcile, open the questions, then the
    `human_task_opened` move when a question opened and the status just reached allows it.
    Returns the listing row (moved or not)."""
    critical = await uow.product_facts.critical_keys()
    await reconcile_tasks(uow, listing)
    asked = await open_questions(
        uow, listing, critical_keys=critical, template=template, images=images
    )
    if asked and can_question_move(listing.status, listing.status_reason):
        listing = await uow.listings.move(
            listing,
            to=QUESTION_MOVE.to,
            event=QUESTION_MOVE.event,
            reason=QUESTION_MOVE.reason,
            actor=actor,
        )
    # SI11 T7: an edit or a validate asks the marketplace's dry run about the current version
    # (only from a pre-approval status; it never fails the request)
    from app.services.remote_preview import request_preview

    await request_preview(uow, listing, listing.current_version_id, trigger="system")
    return listing
