"""Human task inbox routes (owner step S19)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam

from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.tasks import TaskAnswerIn, TaskOut, TaskPage
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import tasks

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["tasks_list"])
async def tasks_list(
    page: Annotated[PageParams, Depends()],
    principal: CurrentPrincipal,
    status: Annotated[Literal["open", "answered", "applied", "cancelled"], Query()] = "open",
) -> TaskPage:
    """The brand's human tasks, newest first; open ones unless `status` says otherwise."""
    rows, more, key = await tasks.list_tasks(principal, status, after=page.after, limit=page.limit)
    return TaskPage(
        data=[TaskOut.model_validate(r) for r in rows],
        page=PageInfo(
            next_cursor=None if key is None else encode_cursor(*key),
            has_more=more,
            limit=page.limit,
        ),
    )


@route(router, OPS["tasks_answer"])
async def tasks_answer(
    task_id: Annotated[UUID, PathParam()],
    body: TaskAnswerIn,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> TaskOut:
    """Answer a fact question (`missing_fact`, `conflicting_facts`): a `value` becomes the
    seller's own fact, `skip: true` writes nothing; the task is then `applied`. The same answer
    again is 200; another answer to a closed task is 409 INVALID_STATUS_TRANSITION. Regenerate
    the listing to use the answer."""
    stored = await tasks.answer_task(principal, idempotency_key, request.url.path, task_id, body)
    return TaskOut.model_validate(stored)
