"""The grouped Inbox (G1): identical open questions and issues across products as one group, and
one answer for many products (`app.services.inbox_groups`). Behind `INBOX_GROUPS_ENABLED`."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.inbox_groups import InboxGroupAnswerIn, InboxGroupAnswerOut, InboxGroupsOut
from app.api.v1.surface import OPS, route
from app.core.config import get_settings
from app.core.rbac import Principal
from app.repositories.analytics import AnalyticsBudgetError
from app.services import inbox_groups

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
DISABLED_MESSAGE = "Answering many products at once is switched off."


@route(router, OPS["inbox_groups_list"])
async def inbox_groups_list(
    principal: CurrentPrincipal,
    view: Annotated[Literal["all", "questions", "issues"], Query()] = "all",
) -> InboxGroupsOut:
    """The open Inbox with identical items across the brand's products in one group: a question
    by its kind, field and allowed answer; an issue by its code, field, marketplace and severity.
    `view=all` is the Inbox's All view (questions, and the issues no open question stands for),
    `questions` the Questions list and `issues` the Issues list, so `total_items` (and the sum of
    the group counts) equals the Home summary's `inbox.open_items`, `inbox.questions` or
    `inbox.issues`. Blocking groups first, then the biggest, then the newest; at most 300
    groups (`more`) and 1,000 members per group (`members_more`). With the switch off,
    `enabled: false` and no groups. A read past its 5 s budget answers 500 `INTERNAL_ERROR`."""
    try:
        body = await inbox_groups.list_groups(principal, view)
    except AnalyticsBudgetError:
        raise ApiError(500, ErrorCode.INTERNAL_ERROR) from None
    return InboxGroupsOut.model_validate(body)


@route(router, OPS["inbox_groups_answer"])
async def inbox_groups_answer(
    body: InboxGroupAnswerIn,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> InboxGroupAnswerOut:
    """One answer for up to 100 questions of one group (`group` is the group's `key`). A product
    that already holds a value is skipped (`has_value`), never overwritten; each value is checked
    against that question's own allowed values (`not_allowed`); each answered product gets the
    same fact and audit rows as a single answer, with the caller as the actor. A `product_fact`
    group takes the value; an `exemption` group takes `true` and marks the products without a
    barcode as GTIN exempt (a barcode is never touched). Any other group, or a task of another
    group, is 422; when every product refuses the value, 422 at `value`. `listings` names the
    affected listings and which can be redrafted now (`POST /listings/{listing_id}/regenerate`);
    nothing is regenerated here. 501 while `INBOX_GROUPS_ENABLED` is off."""
    if not get_settings().inbox_groups_enabled:
        raise ApiError(501, ErrorCode.NOT_IMPLEMENTED, DISABLED_MESSAGE)
    stored = await inbox_groups.answer_group(principal, idempotency_key, request.url.path, body)
    return InboxGroupAnswerOut.model_validate(stored)
