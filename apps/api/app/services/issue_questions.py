"""Issue-sync step 4: one Inbox question per seller-fixable fact or image (SI4 A13, A14;
DATABASE.md:1390-1394 step 4; spec L1015-1037 "ask seller", "one clear question").

`open_questions(uow, listing, *, critical_keys, images)` runs after a check point's issue syncs
(the generation persist, a content edit, a validate of the current version), for our own checks
only, sources `rules_engine` and `vision_model` (CP-D5: adapter questions are phase B):

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

from app.core.logging import get_logger
from app.repositories.attribute_definitions import AttributeDef
from app.repositories.uow import TenantUnitOfWork
from app.services.auto_fix.policy import IMAGE_REPLACEMENT, MISSING_FACT, ask_kind, fact_key, route
from app.services.auto_fix.text import expected_answer, image_question, question_for
from app.services.generation_result import Move

log = get_logger(__name__)

QUESTION_SOURCES: Final = ("rules_engine", "vision_model")
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


async def open_questions(
    uow: TenantUnitOfWork,
    listing: Any,
    *,
    critical_keys: frozenset[str],
    images: Sequence[Any] = (),
) -> list[uuid.UUID]:
    """Open (or reuse) the questions behind this listing's open error rows and link the rows;
    returns the task ids. `images` are the version's `ImageRef`s (`images[<i>]` → image id)."""
    rows = await uow.listing_issues.open_errors(listing.id, QUESTION_SOURCES)
    facts: dict[str, list[Any]] = {}
    pictures: dict[uuid.UUID, list[Any]] = {}
    for row in rows:
        kind = ask_kind(route(_item(row), source=row.source, critical_keys=critical_keys))
        if kind == MISSING_FACT:
            key = fact_key(row.field_path)
            if key is not None:
                facts.setdefault(key, []).append(row)
        elif kind == IMAGE_REPLACEMENT:
            index = _image_index(row.field_path)
            if index is not None and index < len(images):
                pictures.setdefault(images[index].image_id, []).append(row)
    opened: list[uuid.UUID] = []
    if facts:
        known = {d.key: d for d in await uow.attribute_definitions.resolve(facts)}
        for key, group in sorted(facts.items()):
            definition = known.get(key)
            if definition is None or definition.key != key:
                continue  # no dictionary target: the row stays an open issue (P:§2.6)
            question, reason = question_for(
                ErrorCode(group[0].error_code), key, "", value_type=definition.value_type
            )
            task = await uow.human_tasks.open_fact_task(
                listing.product_id,
                fact_target(definition),
                question,
                reason,
                expected_answer(
                    definition.value_type,
                    vocabulary=definition.vocabulary,
                    unit=definition.unit,
                ),
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
    uow: TenantUnitOfWork, listing: Any, actor: Any, images: Sequence[Any]
) -> Any:
    """A human check point's step 4 (edit, validate): reconcile, open the questions, then the
    `human_task_opened` move when a question opened and the status just reached allows it.
    Returns the listing row (moved or not)."""
    critical = await uow.product_facts.critical_keys()
    await reconcile_tasks(uow, listing)
    asked = await open_questions(uow, listing, critical_keys=critical, images=images)
    if not asked or not can_question_move(listing.status, listing.status_reason):
        return listing
    return await uow.listings.move(
        listing,
        to=QUESTION_MOVE.to,
        event=QUESTION_MOVE.event,
        reason=QUESTION_MOVE.reason,
        actor=actor,
    )
