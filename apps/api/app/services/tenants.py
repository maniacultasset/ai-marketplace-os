"""The brand profile (S16 T14; CP3, CP6): `GET /tenants/me` and its update.

The version token is `ETag: "<uuid5(TENANT_ETAG_NS, '<id>:<updated_at>')>"` (CP3: `tenants` has
no version column). `update_profile` checks the patch's voice text with the chunker's seam and
credential scan before any transaction, then in one transaction: the tenant row `FOR UPDATE`,
the `If-Match` compare (stale → 409 `STALE_VERSION`), the merged profile (patch over row) against
`MAX_PROFILE_CHUNKS` (over → 422 `VALIDATION_FAILED` at `profile`), the update of the set fields only, the
`tenant.profile_updated` audit row with the changed keys only, and, when a voice field changed and
no profile job is still `queued` (that row locked, so a worker's claim waits for this commit), one
`brand_embed` profile job. A PATCH storm therefore coalesces into the queued job and never 429s.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import tenant_uow
from app.services.brand_chunking import (
    MAX_PROFILE_CHUNKS,
    ChunkingError,
    check_chunks,
    chunk_profile,
)

TENANT_ETAG_NS: Final = uuid.UUID("6f1c2b7e-1d5a-4c2e-9b8f-5a0d3e7c4b21")
VOICE_FIELDS: Final = ("tone", "guidelines", "glossary")
PROFILE_SOURCE: Final = "brand_profile"


@dataclass(frozen=True, slots=True)
class TenantView:
    id: uuid.UUID
    name: str
    slug: str
    tone: str | None
    guidelines: list[str]
    glossary: dict[str, str]
    status: str


def etag(tenant_id: uuid.UUID, updated_at: datetime) -> str:
    return f'"{uuid.uuid5(TENANT_ETAG_NS, f"{tenant_id}:{updated_at.isoformat()}")}"'


def _view(row: Any) -> TenantView:
    return TenantView(
        id=row.id,
        name=row.name,
        slug=row.slug,
        tone=row.tone,
        guidelines=list(row.guidelines or []),
        glossary=dict(row.glossary or {}),
        status=row.status,
    )


async def get_profile(principal: Principal) -> tuple[TenantView, str]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.tenants.get_profile()
        if row is None:
            raise NotFoundError("tenant")
        return _view(row), etag(row.id, row.updated_at)


def _check_voice(fields: Mapping[str, Any]) -> None:
    """The chunker's seam and credential scan on the patch's own voice text, field by field."""

    def check(field: str, drafts: Any) -> None:
        if not drafts:
            return
        try:
            check_chunks(drafts, field=field, max_chunks=MAX_PROFILE_CHUNKS)
        except ChunkingError:
            raise InputError(ErrorCode.VALIDATION_FAILED, field) from None

    for i, guideline in enumerate(fields.get("guidelines") or []):
        check(f"guidelines[{i}]", chunk_profile(None, [guideline], None))
    if fields.get("glossary"):
        check("glossary", chunk_profile(None, None, fields["glossary"]))
    if fields.get("tone"):
        check("tone", chunk_profile(fields["tone"], None, None))


def _check_merged(before: TenantView, fields: Mapping[str, Any]) -> None:
    """The profile the `brand_embed` job will chunk: the patch overlaid on the locked row (a
    `tone: null` clears it). Per-field caps can each pass while the sum exceeds
    `MAX_PROFILE_CHUNKS`, so the merged drafts are checked before any UPDATE."""
    if not set(fields) & set(VOICE_FIELDS):
        return
    merged = {key: fields[key] if key in fields else _current(before, key) for key in VOICE_FIELDS}
    drafts = chunk_profile(merged["tone"], merged["guidelines"], merged["glossary"])
    if not drafts:
        return
    try:
        check_chunks(drafts, field="profile", max_chunks=MAX_PROFILE_CHUNKS)
    except ChunkingError:
        raise InputError(ErrorCode.VALIDATION_FAILED, "profile") from None


def _current(view: TenantView, key: str) -> Any:
    return getattr(view, key)


async def update_profile(
    principal: Principal, fields: Mapping[str, Any], if_match: str
) -> tuple[TenantView, str]:
    if not fields or set(fields) - {"name", *VOICE_FIELDS}:
        raise ValueError("update_profile takes name, tone, guidelines and glossary only")
    _check_voice(fields)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.tenants.lock_profile()
        if row is None:
            raise NotFoundError("tenant")
        if etag(row.id, row.updated_at) != if_match:
            raise ConflictError(ErrorCode.STALE_VERSION, "tenants_etag")
        before_view = _view(row)
        _check_merged(before_view, fields)
        changed = {k: v for k, v in fields.items() if _current(before_view, k) != v}
        updated = await uow.tenants.update_profile(dict(fields))
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="tenant.profile_updated",
            entity_type="tenant",
            entity_id=principal.tenant_id,
            before=json.dumps({k: _current(before_view, k) for k in changed}, sort_keys=True),
            after=json.dumps(changed, sort_keys=True),
        )
        if set(changed) & set(VOICE_FIELDS) and not await uow.job_runs.has_queued(
            JobKind.BRAND_EMBED, entity_id=principal.tenant_id, source=PROFILE_SOURCE
        ):
            await uow.job_runs.enqueue(
                EnqueueSpec(
                    kind=JobKind.BRAND_EMBED,
                    entity_type="brand_document",
                    entity_id=principal.tenant_id,
                    created_by=principal.user_id,
                    payload={"source": PROFILE_SOURCE},
                )
            )
        return _view(updated), etag(updated.id, updated.updated_at)
