"""Brand documents (owner step S16): upload a guide for chunking, list the live ones, delete one.
Upload and delete need `settings:manage` (owner, admin); the list is `tenant:read`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import JobAccepted
from pydantic import ValidationError

from app.api.errors import ApiError, domain_api_error
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import IdempotencyKey, IfMatch, get_current_principal
from app.api.v1.routers.products import party_out
from app.api.v1.schemas.brand import BrandDocumentOut, BrandDocumentPage, UploadChunkType
from app.api.v1.schemas.brand_profile import BrandProfileIn, BrandProfileOut
from app.api.v1.surface import OPS, route
from app.core.errors import InputError
from app.core.rbac import Principal
from app.services import brand_documents as documents
from app.services import brand_profile
from app.services.intake_values import PartyInput

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["brand_documents_upload"])
async def brand_documents_upload(
    file: Annotated[UploadFile, File(description="The brand document (multipart, 10 MiB cap).")],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    type: Annotated[
        UploadChunkType,
        Form(description="The chunk type of every unit in the file (default `tone_rule`)."),
    ] = "tone_rule",
) -> JobAccepted:
    """Upload a brand document (`.txt` or `.md`, UTF-8); a job chunks and embeds it. The same
    bytes again (under any key) answer the first upload's job."""
    data = await file.read()
    try:
        result = await documents.accept_upload(
            principal, idempotency_key, request.url.path, file.filename, data, type
        )
    except documents.PayloadTooLargeError:
        raise ApiError(413, ErrorCode.PAYLOAD_TOO_LARGE) from None
    except documents.BrandLimitError as exc:
        raise domain_api_error(exc) from None
    if result.status == 422:
        raise InputError(ErrorCode.VALIDATION_FAILED, documents.stored_invalid_field(result.body))
    return JobAccepted.model_validate(result.body)


def _error(report: dict[str, object] | None) -> ErrorReport | None:
    if report is None:
        return None
    try:
        return ErrorReport.model_validate(report)
    except ValidationError:
        return None


@route(router, OPS["brand_documents_list"])
async def brand_documents_list(
    principal: CurrentPrincipal, page: Annotated[PageParams, Depends()]
) -> BrandDocumentPage:
    """The tenant's live brand documents, newest first; a `failed` one carries its `error`."""
    found = await documents.list_documents(principal, limit=page.limit, after=page.after)
    last = found.items[-1] if found.items else None
    next_cursor = encode_cursor(last.created_at, last.id) if found.has_more and last else None
    return BrandDocumentPage(
        data=[
            BrandDocumentOut(
                id=d.id,
                filename=d.filename,
                status=d.status,
                chunk_count=d.chunk_count,
                error=_error(d.error_report),
                created_at=d.created_at,
            )
            for d in found.items
        ],
        page=PageInfo(next_cursor=next_cursor, has_more=found.has_more, limit=page.limit),
    )


@route(router, OPS["brand_documents_delete"])
async def brand_documents_delete(document_id: UUID, principal: CurrentPrincipal) -> Response:
    """Delete a brand document; its chunks stop being used at once. Another tenant's document,
    an unknown id and an already deleted one are the same 404."""
    await documents.delete_document(principal, document_id)
    return Response(status_code=204)


def _profile_out(view: brand_profile.BrandProfileView) -> BrandProfileOut:
    return BrandProfileOut.model_construct(
        default_manufacturer=party_out(view.default_manufacturer),
        website_url=view.website_url,
        updated_at=view.updated_at,
    )


@route(router, OPS["brand_profile_get"])
async def brand_profile_get(principal: CurrentPrincipal, response: Response) -> BrandProfileOut:
    """The brand profile (the default manufacturer every product inherits, and the brand's
    website), with its `ETag`. A brand that never saved one reads as the empty profile."""
    view = await brand_profile.get_brand_profile(principal)
    response.headers["ETag"] = view.etag
    return _profile_out(view)


@route(router, OPS["brand_profile_update"])
async def brand_profile_update(
    body: BrandProfileIn, if_match: IfMatch, principal: CurrentPrincipal, response: Response
) -> BrandProfileOut:
    """Set or clear the default manufacturer or the website (owner, admin). `If-Match` is the
    `ETag` from GET (409 STALE_VERSION when stale); an absent field is unchanged, `null` clears."""
    fields: dict[str, PartyInput | str | None] = {}
    if "default_manufacturer" in body.model_fields_set:
        party = body.default_manufacturer
        fields["default_manufacturer"] = None if party is None else PartyInput(**party.model_dump())
    if "website_url" in body.model_fields_set:
        fields["website_url"] = body.website_url
    view = await brand_profile.update_brand_profile(principal, fields, if_match)
    response.headers["ETag"] = view.etag
    return _profile_out(view)
