"""The ops pages (S14b T22; BUILD-SEQUENCE.md:235): queue, cost, funnel and dead letters, under the
admin mount (`/internal/admin/ops/...`), so the staff gate covers them. Read-only: there is no
requeue control (D10; requeue is the S13 staff CLI). Templates keep Jinja autoescape on.

S23 (C6): the cost page takes `?day=YYYY-MM-DD` (default today UTC; malformed, empty or more than
366 days back is 400) and adds that day's spend per model and per trigger and the top 50 listings
by 30-day cost; `ops/cost/listings/{listing_id}` shows one listing's all-time cost (a malformed id
is 400, an unknown one 404). The numbers come from `app/workers/rollup.py`. Local-only until the
staging admin sits behind IAM (S4): outside local the gate refuses every request."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Final
from uuid import UUID

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from app.admin import ops
from app.admin.session import PlatformSessionMaker
from app.workers import rollup

MAX_DAYS_BACK: Final = 366


def _cost_day(raw: str | None, today: date) -> date | None:
    """`?day=`: absent is today; anything but an ISO date at most 366 days back is None."""
    if raw is None:
        return today
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    if len(raw) != 10 or day < today - timedelta(days=MAX_DAYS_BACK):
        return None
    return day


def _now() -> datetime:
    return datetime.now(UTC)


class OpsView(BaseView):
    name = "Ops"
    identity = "ops"
    icon = "fa-solid fa-gauge"
    clock: ClassVar[Callable[[], datetime]] = staticmethod(_now)

    async def _render(self, request: Request, template: str, **context: Any) -> Response:
        response: Response = await self.templates.TemplateResponse(
            request, template, context={"now": type(self).clock(), **context}
        )
        return response

    @expose("/ops/queue", methods=["GET"])
    async def queue(self, request: Request) -> Response:
        async with PlatformSessionMaker()() as session:
            stats = await ops.queue_stats(session, type(self).clock())
        return await self._render(request, "ops/queue.html", stats=stats)

    @expose("/ops/cost", methods=["GET"])
    async def cost(self, request: Request) -> Response:
        now = type(self).clock()
        day = _cost_day(request.query_params.get("day"), now.astimezone(UTC).date())
        if day is None:
            return PlainTextResponse("day must be YYYY-MM-DD, at most 366 days back", 400)
        async with PlatformSessionMaker()() as session:
            days = await ops.daily_spend(session, now)
            per_listing = await ops.cost_per_published_listing(session, now)
            by_model = await rollup.cost_by_model(session, day)
            by_trigger = await rollup.cost_by_trigger(session, day)
            top = await rollup.cost_per_listing(session, now)
        return await self._render(
            request,
            "ops/cost.html",
            days=days,
            per_listing=per_listing,
            day=day,
            by_model=by_model,
            by_trigger=by_trigger,
            top=top,
        )

    @expose("/ops/cost/listings/{listing_id}", methods=["GET"])
    async def cost_listing(self, request: Request) -> Response:
        try:
            listing_id = UUID(request.path_params["listing_id"])
        except ValueError:
            return PlainTextResponse("listing_id must be a UUID", 400)
        async with PlatformSessionMaker()() as session:
            found = await rollup.listing_cost(session, listing_id)
        if found is None:
            return PlainTextResponse("no such listing", 404)
        return await self._render(request, "ops/cost_listing.html", listing=found)

    @expose("/ops/funnel", methods=["GET"])
    async def funnel(self, request: Request) -> Response:
        async with PlatformSessionMaker()() as session:
            rows = await ops.funnel(session, type(self).clock())
        return await self._render(request, "ops/funnel.html", rows=rows)

    @expose("/ops/dlq", methods=["GET"])
    async def dlq(self, request: Request) -> Response:
        async with PlatformSessionMaker()() as session:
            rows = await ops.dead_letters(session)
        return await self._render(request, "ops/dlq.html", rows=rows)
