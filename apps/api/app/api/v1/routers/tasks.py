"""Human task inbox routes (owner step S19)."""

from __future__ import annotations

from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, Query

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["tasks_list"])
@stub
async def tasks_list(
    page: Annotated[PageParams, Depends()],
    status: Annotated[Literal["open", "answered", "applied", "cancelled"], Query()] = "open",
) -> NoReturn:
    """The brand's human tasks, newest first; open ones unless `status` says otherwise."""
    raise NotImplementedYet("tasks_list")
