"""Rule stubs (owner step S14b)."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Query
from mkos_schemas.enums import Marketplace

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.schemas.listings import DryRunRequest
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["rules_list"])
@stub
async def rules_list(
    page: Annotated[PageParams, Depends()],
    marketplace: Annotated[Marketplace | None, Query()] = None,
    category: Annotated[str | None, Query(pattern=r"^[a-z0-9_]{2,64}$")] = None,
) -> NoReturn:
    """Platform and brand rules, filtered by marketplace or category."""
    raise NotImplementedYet("rules_list")


@route(router, OPS["rules_dry_run"])
@stub
async def rules_dry_run(body: DryRunRequest) -> NoReturn:
    """Run the Rules Engine on a draft listing; nothing is saved."""
    raise NotImplementedYet("rules_dry_run")
