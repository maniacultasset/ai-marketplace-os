"""Rules (owner step S14b): the rule list (T11) and the dry-run (T12)."""

from __future__ import annotations

from typing import Annotated, NoReturn, cast

from fastapi import APIRouter, Depends, Query
from mkos_schemas.enums import Marketplace, Severity

from app.api.errors import NotImplementedYet
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.listings import DryRunRequest
from app.api.v1.schemas.rules import RuleOut, RulePage, RuleType
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services import rules_catalog

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _out(view: rules_catalog.RuleView) -> RuleOut:
    """A DB row is built, not validated: the DB CHECKs are the read-side truth (as products)."""
    return RuleOut.model_construct(
        id=view.id,
        marketplace=Marketplace(view.marketplace),
        category=view.category,
        field=view.field,
        rule_type=cast(RuleType, view.rule_type),
        severity=Severity(view.severity),
        version=view.version,
        enabled=view.enabled,
        scope=view.scope,
    )


@route(router, OPS["rules_list"])
async def rules_list(
    page: Annotated[PageParams, Depends()],
    principal: CurrentPrincipal,
    marketplace: Annotated[Marketplace | None, Query()] = None,
    category: Annotated[str | None, Query(pattern=r"^[a-z0-9_]{2,64}$")] = None,
) -> RulePage:
    """Platform and brand rules, filtered by marketplace or category."""
    views, has_more = await rules_catalog.list_rules(
        principal,
        limit=page.limit,
        after=page.after,
        marketplace=None if marketplace is None else marketplace.value,
        category=category,
    )
    last = views[-1] if views else None
    next_cursor = encode_cursor(last.created_at, last.id) if has_more and last else None
    return RulePage(
        data=[_out(v) for v in views],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
    )


@route(router, OPS["rules_dry_run"])
@stub
async def rules_dry_run(body: DryRunRequest) -> NoReturn:
    """Run the Rules Engine on a draft listing; nothing is saved."""
    raise NotImplementedYet("rules_dry_run")
