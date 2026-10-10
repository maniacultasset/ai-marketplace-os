"""`GET /api/v1/listings/{listing_id}/submissions` (S21 T24; PLAN D3; T46): the listing's publish
attempts, newest first by `(created_at, id)`, each with its items and per-field results. A listing
of another tenant (or none) is 404 before anything else; storage keys, digests, idempotency keys
and raw bodies never leave the server."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.repositories.uow import tenant_uow


def _field(row: Any) -> dict[str, Any]:
    return {"field": row.field, "status": row.status, "error": row.error_json}


async def page(
    principal: Principal,
    listing_id: uuid.UUID,
    *,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], tuple[datetime, uuid.UUID] | None, bool]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        if await uow.listings.get(listing_id) is None:
            raise NotFoundError("listing_id")
        rows, more = await uow.submissions.page(listing_id, after=after, limit=limit)
        results = await uow.field_results.for_submissions([r.id for r in rows])
        items = {i.id: i for i in await uow.listing_items.for_listing(listing_id)}
    by_submission: dict[uuid.UUID, dict[uuid.UUID, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for result in results:
        by_submission[result.submission_id][result.listing_item_id].append(_field(result))
    data = []
    for row in rows:
        per_item = by_submission.get(row.id, {})
        data.append(
            {
                "id": row.id,
                "listing_version_id": row.listing_version_id,
                "attempt": row.attempt,
                "operation": row.operation,
                "field_paths": row.field_paths,
                "outcome": row.outcome,
                "error_code": row.error_code,
                "retry_justification": row.retry_justification,
                "requested_by_user_id": row.requested_by_user_id,
                "items": [
                    {
                        "listing_item_id": item.id,
                        "seller_sku": item.seller_sku,
                        "marketplace_state": item.marketplace_state,
                        "fields": per_item.get(item.id, []),
                    }
                    for item in items.values()
                    if item.id in per_item or not per_item
                ],
                "error": row.error_report_json,
                "sent_at": row.sent_at,
                "responded_at": row.responded_at,
                "resolved_at": row.resolved_at,
                "created_at": row.created_at,
            }
        )
    last = rows[-1] if rows and more else None
    return data, None if last is None else (last.created_at, last.id), more
