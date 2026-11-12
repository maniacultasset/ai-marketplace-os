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
4. The same draft asked again (the same capped text and the same offered categories, so the same
   `input_digest`) answers the earlier suggestion instead of paying for another call: 202 with
   its id while it runs, 200 with its candidates when it was `suggested` within the last day.
   Checked before the admission, and again under the tenant AI lock inside the key.
5. Else one `image_compliance` job (entity `category_suggestion`, under the tenant AI lock)
   whose payload names `category_draft` with the draft text, capped exactly as a product's
   (`classify_text`) and then fitted to `PAYLOAD_BUDGET` encoded bytes (attribute values are
   dropped from the end, then the description is cut), plus its digest. A draft has no product
   row: the entity id is derived from the digest (`draft_entity_id`), so the same draft maps to
   one entity and its newest job is the indexed `latest_for_entity`. The job body is
   `app.workers.category_draft`; its answer is the job's `result_json`.

The draft text in the payload is the text the seller is about to save on the product (the same
title, description, brand and attribute values), which the read-only roles can read there too.

The read answers `queued` while the job is live, the stored answer once it succeeded, and
`failed` when it failed or was cancelled. Another tenant's suggestion, a product suggestion's
job and any other job read as missing (404).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from mkos_schemas.classify import CLASSIFY_CONFIDENCE_TASK_THRESHOLD
from mkos_schemas.enums import JobKind

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.ops import JobRun
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services.budget import admit, refuse
from app.services.catalog_categories import category_label
from app.services.category_gate import category_offers
from app.services.category_suggest import (
    DESCRIPTION_CHARS,
    MAX_ATTRIBUTES,
    TEXT_CHARS,
    TITLE_CHARS,
    compute_digest,
    peek_replay,
)
from app.services.idempotency import idempotent
from app.workers.budget import classify_reservation_usd
from app.workers.policy import CATEGORY_DRAFT_PAYLOAD as DRAFT_KEY
from app.workers.policy import CATEGORY_SUGGESTION_ENTITY, lock_for

__all__ = [
    "DRAFT_KEY",
    "PAYLOAD_BUDGET",
    "REUSE_WINDOW",
    "Draft",
    "answer_from_job",
    "draft_digest",
    "draft_entity_id",
    "draft_text",
    "encoded_bytes",
    "fit_text",
    "read_draft_suggestion",
    "request_draft_suggestion",
]

log = get_logger(__name__)
METHOD: Final = "POST"
LIVE: Final = frozenset({"queued", "running"})
ANSWERS: Final = frozenset({"suggested", "unavailable", "failed"})
# The job payload's limit is 64 KiB (`repositories.jobs.MAX_PAYLOAD_BYTES`, ASCII-escaped JSON:
# a character outside the BMP costs 12 bytes). The capped text can pass it (about 10,700
# characters), so it is fitted to this budget, with room left for the payload's keys.
PAYLOAD_BUDGET: Final = 48 * 1024
REUSE_WINDOW: Final = timedelta(days=1)
_DRAFT_NAMESPACE: Final = uuid.UUID("7c1d0a52-5d55-4f1e-9a9b-2f7d3c6b8e10")


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


def encoded_bytes(value: object) -> int:
    """The size `JobRunRepository.enqueue` measures (pure): compact, ASCII-escaped JSON."""
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def fit_text(text: Mapping[str, Any], budget: int = PAYLOAD_BUDGET) -> dict[str, Any]:
    """The draft text within `budget` encoded bytes (pure): attribute values are dropped from the
    last key back, then the description is cut by halves. The title (500 characters) and the
    brand (200) always fit. A text already within the budget comes back unchanged."""
    fitted: dict[str, Any] = {**text, "attributes": dict(text.get("attributes") or {})}
    attributes: dict[str, str] = fitted["attributes"]
    while encoded_bytes(fitted) > budget and attributes:
        attributes.pop(sorted(attributes)[-1])
    while encoded_bytes(fitted) > budget and fitted.get("description"):
        description = str(fitted["description"])
        fitted["description"] = description[: len(description) // 2].rstrip() or None
    return fitted


def draft_digest(text: Mapping[str, Any], categories: Sequence[str]) -> str:
    """The digest of the fitted text and the offered categories (pure), as a product's
    `input_digest` with no photos."""
    return compute_digest(text, [], categories)


def draft_entity_id(digest: str) -> uuid.UUID:
    """The job's entity id for a draft (pure): the same digest maps to the same id."""
    return uuid.uuid5(_DRAFT_NAMESPACE, digest)


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


async def _earlier(
    uow: TenantUnitOfWork, digest: str, now: datetime
) -> tuple[int, dict[str, Any]] | None:
    """The newest suggestion of this draft when it can answer again: 202 while its job is live,
    200 with its candidates when it was `suggested` within `REUSE_WINDOW`; else None (a failed or
    unavailable one is asked again)."""
    job: JobRun | None = await uow.job_runs.latest_for_entity(
        CATEGORY_SUGGESTION_ENTITY, draft_entity_id(digest)
    )
    if job is None or not isinstance(job.payload_json, Mapping):
        return None
    stored = job.payload_json.get(DRAFT_KEY)
    if not isinstance(stored, Mapping) or stored.get("digest") != digest:
        return None
    if job.status in LIVE:
        return 202, _body("queued", job.id)
    if job.status != "succeeded" or job.queued_at < now - REUSE_WINDOW:
        return None
    answer = answer_from_job(job.id, job.status, job.result_json)
    return (200, answer) if answer["status"] == "suggested" else None


async def request_draft_suggestion(
    principal: Principal, idem_key: str, path: str, draft: Draft
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    settings = get_settings()
    text = fit_text(draft_text(draft))
    payload = json.dumps(text, sort_keys=True, ensure_ascii=False).encode()
    replay = await peek_replay(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    if not settings.category_suggest_enabled:
        log.info("category_draft.disabled", tenant_id=str(tenant))
        return 200, _body("unavailable")
    now = datetime.now(UTC)
    async with tenant_uow(tenant, user_id=principal.user_id) as uow:
        offers = await category_offers(uow, settings)
        digest = draft_digest(text, [offer.category for offer in offers])
        fast = await _earlier(uow, digest, now) if offers else None
    if not offers:
        return 200, _body("unavailable")
    if fast is not None:
        log.info("category_draft.reused", tenant_id=str(tenant), job_id=fast[1]["suggestion_id"])
        return fast
    refuse(await admit(tenant, now, amount=classify_reservation_usd(settings)), now)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        await uow.images.lock_tenant_image_jobs()
        again = await _earlier(uow, digest, now)
        if again is not None:
            return again
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type=CATEGORY_SUGGESTION_ENTITY,
                entity_id=draft_entity_id(digest),
                created_by=principal.user_id,
                payload={DRAFT_KEY: {"text": text, "digest": digest}},
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
