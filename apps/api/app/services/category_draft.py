"""Client feedback 11 Nov (item 5): suggest a category before the product is saved.

`POST /catalog/category-suggestions` classifies a draft (the name, description, brand and
attribute text the seller typed so far) into up to 3 of the offered categories; `GET
/catalog/category-suggestions/{suggestion_id}` reads the answer. The seller picks one; the model
never sets a category, and nothing is written to a product.

The ask follows the product path (`category_suggest.request_suggestion`):
1. A completed replay of the Idempotency-Key answers its stored status and body; another body
   under the key is 409 `IDEMPOTENCY_KEY_REUSED`.
2. While `CATEGORY_SUGGEST_ENABLED` is off, or no category has a current template, it answers 200
   `unavailable`: no job, no admission, no spend, nothing stored.
3. The SI10 admission, outside any unit of work: AI paused is 503 with the pause's code, a daily
   cap 429 `BUDGET_EXCEEDED`. Nothing is written on a refusal.
4. One `image_compliance` job (entity `category_suggestion`, under the tenant AI lock) whose
   payload names `category_draft` with the draft text, capped exactly as a product's
   (`classify_text`). The entity id is a fresh id: a draft has no product row. The job body is
   `app.workers.category_draft`; its answer is the job's `result_json`.

The read answers `queued` while the job is live, the stored answer once it succeeded, and
`failed` when it failed or was cancelled. Another tenant's suggestion, a product suggestion's
job and any other job read as missing (404).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.classify import CLASSIFY_CONFIDENCE_TASK_THRESHOLD
from mkos_schemas.enums import JobKind

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import repositories_on, tenant_uow
from app.services.budget import admit, refuse
from app.services.catalog_categories import category_label
from app.services.category_gate import category_offers
from app.services.category_suggest import (
    DESCRIPTION_CHARS,
    MAX_ATTRIBUTES,
    TEXT_CHARS,
    TITLE_CHARS,
    peek_replay,
)
from app.services.idempotency import idempotent
from app.workers.budget import classify_reservation_usd
from app.workers.policy import CATEGORY_DRAFT_PAYLOAD as DRAFT_KEY
from app.workers.policy import CATEGORY_SUGGESTION_ENTITY, lock_for

__all__ = [
    "DRAFT_KEY",
    "Draft",
    "answer_from_job",
    "draft_text",
    "read_draft_suggestion",
    "request_draft_suggestion",
]

log = get_logger(__name__)
METHOD: Final = "POST"
LIVE: Final = frozenset({"queued", "running"})
ANSWERS: Final = frozenset({"suggested", "unavailable", "failed"})


@dataclass(frozen=True, slots=True)
class Draft:
    """What the seller typed so far (the request body)."""

    title: str
    description: str | None = None
    brand: str | None = None
    attributes: Mapping[str, str] | None = None


def _short(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value.strip()[:limit] or None


def draft_text(draft: Draft) -> dict[str, Any]:
    """The draft as `ProductText`, capped as a product's classify text (pure): the title, the
    description, the brand and up to 40 non-empty attribute values in key order. The seller's
    category words are not asked for: the category is what is being suggested."""
    attributes: dict[str, str] = {}
    for key in sorted(draft.attributes or {}):
        if len(attributes) >= MAX_ATTRIBUTES:
            break
        value = _short((draft.attributes or {})[key], TEXT_CHARS)
        if value is not None:
            attributes[key] = value
    return {
        "title": (draft.title or "").strip()[:TITLE_CHARS] or "untitled",
        "description": _short(draft.description, DESCRIPTION_CHARS),
        "brand": _short(draft.brand, TEXT_CHARS),
        "seller_category": None,
        "attributes": attributes,
    }


def _body(
    status: str,
    suggestion_id: uuid.UUID | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "suggestion_id": None if suggestion_id is None else str(suggestion_id),
        "status": status,
        "candidates": candidates or [],
    }


def _candidates(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = result.get("candidates")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("category"), str):
            continue
        confidence = float(item.get("confidence") or 0.0)
        out.append(
            {
                "category": item["category"],
                "label": category_label(item["category"]),
                "confidence": confidence,
                "rationale": str(item.get("rationale") or ""),
                "unsure": confidence < CLASSIFY_CONFIDENCE_TASK_THRESHOLD,
            }
        )
    return out


def answer_from_job(
    job_id: uuid.UUID, status: str, result: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The read's body from the job's status and `result_json` (pure)."""
    if status in LIVE:
        return _body("queued", job_id)
    if status != "succeeded":
        return _body("failed", job_id)
    stored = result if isinstance(result, Mapping) else {}
    answer = stored.get("status")
    if answer not in ANSWERS:
        return _body("failed", job_id)
    return _body(str(answer), job_id, _candidates(stored) if answer == "suggested" else [])


async def request_draft_suggestion(
    principal: Principal, idem_key: str, path: str, draft: Draft
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    settings = get_settings()
    text = draft_text(draft)
    payload = json.dumps(text, sort_keys=True, ensure_ascii=False).encode()
    replay = await peek_replay(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    if not settings.category_suggest_enabled:
        log.info("category_draft.disabled", tenant_id=str(tenant))
        return 200, _body("unavailable")
    async with tenant_uow(tenant, user_id=principal.user_id) as uow:
        offers = await category_offers(uow, settings)
    if not offers:
        return 200, _body("unavailable")
    now = datetime.now(UTC)
    refuse(await admit(tenant, now, amount=classify_reservation_usd(settings)), now)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type=CATEGORY_SUGGESTION_ENTITY,
                entity_id=uuid.uuid4(),
                created_by=principal.user_id,
                payload={DRAFT_KEY: {"text": text}},
                lock=lock_for(JobKind.IMAGE_COMPLIANCE, CATEGORY_SUGGESTION_ENTITY, tenant),
            )
        )
        return 202, _body("queued", enqueued.job.id)

    status, body = await idempotent(principal, idem_key, METHOD, path, payload, handler)
    log.info(
        "category_draft.requested",
        tenant_id=str(tenant),
        job_id=body.get("suggestion_id"),
        status=body.get("status"),
    )
    return status, body


async def read_draft_suggestion(principal: Principal, suggestion_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        job = await uow.job_runs.get_by_id(suggestion_id)
        if (
            job is None
            or job.entity_type != CATEGORY_SUGGESTION_ENTITY
            or not isinstance(job.payload_json, Mapping)
            or DRAFT_KEY not in job.payload_json
        ):
            raise NotFoundError("suggestion_id")
        return answer_from_job(job.id, job.status, job.result_json)
