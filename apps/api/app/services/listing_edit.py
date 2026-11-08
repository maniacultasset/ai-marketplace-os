"""A human content edit (S18 T23; D13; CP7; DATABASE.md:1384, :1461).

`edit_content(uow, principal, listing_id, if_match, patch)`, under the listing's row lock:

1. the status: `generating` → 409 `LISTING_BUSY`; `created` or no current version → 409
   `INVALID_STATUS_TRANSITION` (CP7: regenerate instead); paused/archived → 409
   `LISTING_PAUSED|LISTING_ARCHIVED`; `if_match` ≠ the current version → 409 `STALE_VERSION`;
2. the new listing: `content` replaced, `keywords` and `attributes` only when sent, images kept;
3. no change → the current detail, no version;
4. otherwise a version (`origin=edit`, the parent, the top-level `changed_paths`, the parent's
   maker kept as `generated_by_user_id`, the caller as `edited_by_user_id`, the parent's pins);
5. the Rules Engine on it, one run (attempt 1, linked); SI9 B4: the marketplace check on the
   new content before the insert, its items synced under `schema` (cause `edit`);
6. the move (event `edit`) to `validated`, `needs_review` or `blocked/validation_errors`, the
   approval cleared (an edit voids it; the editor can no longer approve, lv_guard);
7. SI4 A13: the questions (`ask_after_check`): tasks whose issues all closed are cancelled, and
   an open error row only the seller can fix opens or reuses one task; then the
   `human_task_opened` move to `blocked/human_task_open`. An edit is never auto-fixed (D6).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ListingStatus
from mkos_schemas.listing import CanonicalListing, ListingContent, ListingKeywords
from pydantic import ValidationError

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.listings import NewVersion, SourceVersion, UserActor
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.repositories.validation_runs import summary_of
from app.services.adapter_check import adapter_check
from app.services.generation_context import previous_listing
from app.services.issue_questions import ask_after_check
from app.services.issues import sync_issues
from app.services.listings import human_move, insert_run, rules_check, to_detail
from app.services.seller_facts import FACTUAL_KEYS

log = get_logger(__name__)


def _gate(listing: Any, if_match: uuid.UUID) -> None:
    if listing.status == "generating":
        raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
    if getattr(listing, "paused_at", None) is not None:
        raise ConflictError(ErrorCode.LISTING_PAUSED, "listings")
    if getattr(listing, "archived_at", None) is not None:
        raise ConflictError(ErrorCode.LISTING_ARCHIVED, "listings")
    if listing.status == "created" or listing.current_version_id is None:
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings")
    if if_match != listing.current_version_id:
        raise ConflictError(ErrorCode.STALE_VERSION, "listing_versions")


def _factual_edit(old: CanonicalListing, new: CanonicalListing) -> str | None:
    """SI2 T17 (D5, CP-D7): the first factual attribute (sorted) the edit changes, adds or
    removes. A factual value is the seller's, edited on the product, never on the listing."""
    touched = sorted(k for k in FACTUAL_KEYS if old.attributes.get(k) != new.attributes.get(k))
    return f"attributes.{touched[0]}" if touched else None


def _changed(old: CanonicalListing, new: CanonicalListing) -> list[str]:
    return [
        path
        for path in ("content", "keywords", "attributes")
        if getattr(old, path) != getattr(new, path)
    ]


async def edit_content(
    uow: TenantUnitOfWork,
    principal: Principal,
    listing_id: uuid.UUID,
    if_match: uuid.UUID,
    *,
    content: ListingContent,
    keywords: ListingKeywords | None,
    attributes: dict[str, Any] | None,
) -> dict[str, Any]:
    listing = await uow.listings.get_for_update(listing_id)
    if listing is None:
        raise NotFoundError("listing_id")
    _gate(listing, if_match)
    parent = await uow.listing_versions.current(listing)
    old = previous_listing(listing, parent)
    if parent is None or old is None:
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_versions")
    update: dict[str, Any] = {"content": content}
    if keywords is not None:
        update["keywords"] = keywords
    if attributes is not None:
        update["attributes"] = attributes
    try:
        new = CanonicalListing.model_validate({**old.model_dump(), **update})
    except ValidationError as exc:
        # The patch schema is looser than the canonical caps (128 attributes, 256-char values):
        # a 422 on the first failing path, the value never echoed.
        loc = exc.errors(include_input=False)[0]["loc"]
        raise InputError(ErrorCode.VALIDATION_FAILED, ".".join(str(p) for p in loc)) from None
    factual = _factual_edit(old, new)
    if factual is not None:
        # 422 VALIDATION_FAILED at the attribute (the declared code; the value never echoed)
        raise InputError(ErrorCode.VALIDATION_FAILED, factual)
    changed = _changed(old, new)
    if not changed:
        summary = await uow.validation_runs.summary_for(parent.id)
        return to_detail(listing, old, summary)

    from_status = listing.status
    number = await uow.listing_versions.next_version(listing.id)
    check = await rules_check(uow, listing, new)
    # SI9 B4 (CP-D2): the marketplace check on the new content before the insert
    new_id = uuid.uuid4()
    adapter = await adapter_check(
        uow,
        listing.id,
        version=SourceVersion(
            new_id, number, new.model_dump(mode="json"), parent.category_template_id
        ),
    )
    to, reason = human_move(check.report, adapter)
    stored = new.model_copy(
        update={"version": number, "status": ListingStatus(to), "created_at": None}
    )
    version = await uow.listing_versions.insert(
        NewVersion(
            id=new_id,
            listing_id=listing.id,
            version=number,
            content_json=stored.model_dump(mode="json"),
            image_reports_json=list(parent.image_reports_json or []),
            generated_by_user_id=parent.generated_by_user_id,
            edited_by_user_id=principal.user_id,
            origin="edit",
            parent_version_id=parent.id,
            changed_paths=changed,
            locale=parent.locale,
            rules_version=parent.rules_version,
            category_template_id=parent.category_template_id,
        )
    )
    run = await insert_run(uow, listing, version.id, check, cause="edit")
    if adapter is not None:
        await sync_issues(
            uow,
            listing,
            "schema",
            adapter,
            run_id=None,
            cause="edit",
            version_id=version.id,
            critical_keys=await uow.product_facts.critical_keys(),
        )
    listing = await uow.listings.move(
        listing,
        to=to,
        event="edit",
        reason=reason,
        actor=UserActor(principal.user_id),
        current_version_id=version.id,
        clear_approval=True,
    )
    listing = await ask_after_check(
        uow, listing, UserActor(principal.user_id), stored.images, template=check.template
    )
    # `audit()` keeps only the listing's allow-listed keys (0004: status, status_reason, ...);
    # the version link is in listing_versions (origin=edit, parent_version_id).
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="listing.edited",
        entity_type="listing",
        entity_id=listing.id,
        before=json.dumps({"status": from_status}),
        after=json.dumps({"status": to, "status_reason": reason}),
    )
    log.info(
        "listing.edited",
        listing_id=str(listing.id),
        version_id=str(version.id),
        status=to,
        changed=len(changed),
    )
    return to_detail(listing, stored, summary_of([run]))


async def edit(
    principal: Principal,
    listing_id: uuid.UUID,
    if_match: uuid.UUID,
    *,
    content: ListingContent,
    keywords: ListingKeywords | None,
    attributes: dict[str, Any] | None,
) -> dict[str, Any]:
    """The route's transaction: the caller's unit of work (the user is the move's actor)."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await edit_content(
            uow,
            principal,
            listing_id,
            if_match,
            content=content,
            keywords=keywords,
            attributes=attributes,
        )
