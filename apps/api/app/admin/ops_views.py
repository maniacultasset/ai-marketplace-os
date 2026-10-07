"""The ops pages (S14b T22; BUILD-SEQUENCE.md:235): queue, cost, funnel and dead letters, under the
admin mount (`/internal/admin/ops/...`), so the staff gate covers them. Read-only: there is no
requeue control (D10; requeue is the S13 staff CLI). Templates keep Jinja autoescape on."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

from app.admin import ops
from app.admin.session import PlatformSessionMaker


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
        async with PlatformSessionMaker()() as session:
            days = await ops.daily_spend(session, now)
            per_listing = await ops.cost_per_published_listing(session, now)
        return await self._render(request, "ops/cost.html", days=days, per_listing=per_listing)

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
